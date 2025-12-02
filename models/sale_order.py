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
        Versión: FORZADO (Si dice 'in_payment', se paga aunque el residual diga lo contrario).
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        debug_logs = []

        _logger.info(f"[COMMISSION] --- Inicio recálculo para: {self.name} ---")

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            return self._return_notification("Sin facturas válidas.", "warning")

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        for inv in invoices:
            if inv.payment_state == 'not_paid':
                continue

            _logger.info(f"[COMMISSION] Factura: {inv.name} | Estado: {inv.payment_state}")
            
            payments_found = []

            # ---------------------------------------------------------
            # ESTRATEGIA 1: Widget de Pagos
            # ---------------------------------------------------------
            if inv.invoice_payments_widget:
                try:
                    data = inv.invoice_payments_widget
                    content = data.get('content', []) if isinstance(data, dict) else []
                    if content:
                        for pay_info in content:
                            payments_found.append({
                                'amount': pay_info.get('amount', 0.0),
                                'payment_id': pay_info.get('account_payment_id', False),
                                'ref': pay_info.get('ref', 'Vía Widget')
                            })
                        _logger.info(f"[COMMISSION] -> Hallazgo Estrategia 1 (Widget): {len(content)} pagos.")
                except Exception:
                    pass

            # ---------------------------------------------------------
            # ESTRATEGIA 2: Búsqueda Física
            # ---------------------------------------------------------
            if not payments_found:
                invoice_move_lines = inv.line_ids
                partials = self.env['account.partial.reconcile'].search([
                    '|',
                    ('debit_move_id', 'in', invoice_move_lines.ids),
                    ('credit_move_id', 'in', invoice_move_lines.ids)
                ])
                if partials:
                    for partial in partials:
                        payments_found.append({
                            'amount': partial.amount,
                            'payment_id': False, 
                            'ref': f"Conciliación ID {partial.id}"
                        })

            # ---------------------------------------------------------
            # ESTRATEGIA 3: Red de Seguridad Matemática
            # ---------------------------------------------------------
            if not payments_found and inv.amount_total > 0:
                paid_amount = inv.amount_total - inv.amount_residual
                if paid_amount > 0.01:
                    payments_found.append({
                        'amount': paid_amount,
                        'payment_id': False,
                        'ref': 'Saldo Saldado (Cálculo Matemático)'
                    })

            # ---------------------------------------------------------
            # ESTRATEGIA 4: MODO FORZADO (La solución a tu problema)
            # ---------------------------------------------------------
            # Si llegamos aquí vacíos, pero el estado es 'in_payment' o 'paid',
            # asumimos que es un error de Odoo actualizando el residual y forzamos el total.
            if not payments_found and inv.payment_state in ['in_payment', 'paid']:
                msg = f"Factura {inv.name}: Residual no bajó, pero estado es '{inv.payment_state}'. FORZANDO PAGO TOTAL."
                _logger.warning(f"[COMMISSION] -> {msg}")
                debug_logs.append(msg)
                
                payments_found.append({
                    'amount': inv.amount_total,
                    'payment_id': False,
                    'ref': 'Forzado por Estado Visual'
                })

            if not payments_found:
                _logger.warning(f"[COMMISSION] SALTAR: {inv.name} no parece tener pagos procesables.")
                continue

            # ---------------------------------------------------------
            # GENERACIÓN
            # ---------------------------------------------------------
            is_refund = (inv.move_type == 'out_refund')
            sign = -1 if is_refund else 1

            for pay_data in payments_found:
                amount_paid = pay_data['amount']
                payment_id = pay_data['payment_id']
                
                if amount_paid <= 0.01: continue
                if inv.amount_total == 0: continue

                ratio = amount_paid / inv.amount_total
                amount_paid_signed = amount_paid * sign

                for rule in self.commission_rule_ids:
                    # Candado Anti-Duplicados
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
                        _logger.info(f"[COMMISSION] Ignorando duplicado para {rule.partner_id.name}")
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
                            'name': f"Cmsn: {inv.name} ({pay_data['ref']})"
                        })
                        created_count += 1
                        debug_logs.append(f"+ {comm_amount} ({rule.partner_id.name})")

        msg_type = "success" if created_count > 0 else "info"
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