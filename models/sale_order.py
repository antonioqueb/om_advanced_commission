from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')

    def action_recalc_commissions(self):
        """
        Botón de emergencia/manual para generar comisiones.
        Versión: HÍBRIDA (Busca conciliación contable y si falla, usa el Widget JSON).
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        debug_logs = []

        _logger.info(f"[COMMISSION-DEBUG] Iniciando recálculo para Orden: {self.name}")

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            return self._return_notification("Sin facturas válidas.", "warning")

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        for inv in invoices:
            _logger.info(f"[COMMISSION-DEBUG] Procesando Factura: {inv.name} | Estado: {inv.payment_state}")

            if inv.payment_state == 'not_paid':
                continue

            # Lista donde acumularemos los pagos encontrados para esta factura
            # Formato: {'amount': float, 'payment_id': int|False, 'date': date}
            payments_found = []

            # ---------------------------------------------------------
            # ESTRATEGIA A: Búsqueda Estricta Contable (Partials)
            # ---------------------------------------------------------
            receivable_lines = inv.line_ids.filtered(lambda l: l.account_type == 'asset_receivable')
            partials = self.env['account.partial.reconcile']
            
            for line in receivable_lines:
                partials |= line.matched_credit_ids # Si es factura cliente (Debe) -> busca Haber
                partials |= line.matched_debit_ids  # Si es nota crédito (Haber) -> busca Debe

            if partials:
                _logger.info(f"[COMMISSION-DEBUG] ESTRATEGIA A: Encontradas {len(partials)} conciliaciones contables.")
                for partial in partials:
                    # Determinar línea contrapartida
                    if partial.debit_move_id in inv.line_ids:
                        counterpart = partial.credit_move_id
                    else:
                        counterpart = partial.debit_move_id
                    
                    pay_obj = self.env['account.payment'].search([('move_id', '=', counterpart.move_id.id)], limit=1)
                    
                    payments_found.append({
                        'amount': partial.amount,
                        'payment_id': pay_obj.id if pay_obj else False,
                        'source': 'accounting'
                    })

            # ---------------------------------------------------------
            # ESTRATEGIA B: Fallback via Widget JSON (Si A falló)
            # ---------------------------------------------------------
            if not payments_found and inv.invoice_payments_widget:
                _logger.info(f"[COMMISSION-DEBUG] ESTRATEGIA B: Usando Widget JSON (Fallback).")
                try:
                    # invoice_payments_widget suele ser un dict en el backend, pero prevenimos si es string
                    widget_data = inv.invoice_payments_widget
                    if widget_data and isinstance(widget_data, dict) and 'content' in widget_data:
                        for payment_info in widget_data['content']:
                            # El widget contiene: amount, date, account_payment_id, journal_name...
                            p_id = payment_info.get('account_payment_id', False)
                            p_amount = payment_info.get('amount', 0.0)
                            
                            payments_found.append({
                                'amount': p_amount,
                                'payment_id': p_id,
                                'source': 'widget'
                            })
                except Exception as e:
                    _logger.error(f"[COMMISSION-DEBUG] Error leyendo widget JSON: {e}")

            if not payments_found:
                msg = f"Factura {inv.name}: Pagada pero no se pudo determinar el origen del pago (ni contabilidad ni widget)."
                _logger.warning(f"[COMMISSION-DEBUG] {msg}")
                debug_logs.append(msg)
                continue

            # ---------------------------------------------------------
            # GENERACIÓN DE COMISIONES
            # ---------------------------------------------------------
            is_refund = (inv.move_type == 'out_refund')
            sign = -1 if is_refund else 1

            for pay_data in payments_found:
                amount_paid = pay_data['amount']
                payment_id = pay_data['payment_id']
                
                _logger.info(f"[COMMISSION-DEBUG] Procesando Pago: {amount_paid} (ID: {payment_id})")

                if inv.amount_total == 0:
                    continue

                ratio = amount_paid / inv.amount_total
                amount_paid_signed = amount_paid * sign

                for rule in self.commission_rule_ids:
                    # Candado Anti-Duplicados (Range Float Match)
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('base_amount_paid', '>=', amount_paid_signed - 0.02),
                        ('base_amount_paid', '<=', amount_paid_signed + 0.02),
                    ]
                    
                    if payment_id:
                        domain.append(('payment_id', '=', payment_id))
                    else:
                        domain.append(('payment_id', '=', False))
                        
                    existing = CommissionMove.search(domain)
                    if existing:
                        _logger.info(f"[COMMISSION-DEBUG] Ignorado: Duplicado para {rule.partner_id.name}")
                        continue

                    # Cálculo
                    comm_amount = 0.0
                    if rule.calculation_base == 'manual':
                        comm_amount = rule.fixed_amount * ratio
                    else:
                        comm_amount = rule.estimated_amount * ratio
                    
                    comm_amount *= sign

                    if abs(comm_amount) > 0.01:
                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': self.id,
                            'payment_id': payment_id or False,
                            'invoice_line_id': inv.invoice_line_ids[0].id if inv.invoice_line_ids else False,
                            'amount': comm_amount,
                            'base_amount_paid': amount_paid_signed,
                            'currency_id': self.currency_id.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {inv.name}"
                        })
                        created_count += 1
                        debug_logs.append(f"+ {comm_amount} ({rule.partner_id.name})")

        # Notificación Final
        msg_type = "success" if created_count > 0 else "warning"
        final_msg = f"Proceso finalizado. {created_count} comisiones generadas."
        if debug_logs:
             final_msg += "\n" + "\n".join(debug_logs)

        return self._return_notification(final_msg, msg_type)

    def _return_notification(self, message, type='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Gestión de Comisiones',
                'message': message,
                'type': type,
                'sticky': False,
            }
        }

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')