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
        Versión: AGRESIVA (Prioriza el Widget de Pagos y acepta asientos manuales).
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        debug_logs = []

        _logger.info(f"[COMMISSION] Iniciando recálculo para: {self.name}")

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            return self._return_notification("Sin facturas válidas.", "warning")

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        for inv in invoices:
            # Si no está pagada ni en proceso, la saltamos.
            if inv.payment_state == 'not_paid':
                continue

            _logger.info(f"[COMMISSION] Analizando Factura: {inv.name} | Estado: {inv.payment_state}")

            # Lista de pagos encontrados: [{'amount': 100.0, 'payment_id': 5 (o False), 'ref': 'Pago X'}]
            payments_found = []

            # ---------------------------------------------------------
            # MÉTODO 1: Widget de Pagos (Fuente de verdad de Odoo)
            # ---------------------------------------------------------
            # Este campo contiene el JSON que pinta la línea verde en la factura
            if inv.invoice_payments_widget:
                try:
                    data = inv.invoice_payments_widget
                    # En Odoo modernos es un dict, si es string lo convertimos (raro en v19)
                    content = data.get('content', []) if isinstance(data, dict) else []
                    
                    if content:
                        _logger.info(f"[COMMISSION] Encontrados {len(content)} pagos vía Widget JSON.")
                        for pay_info in content:
                            payments_found.append({
                                'amount': pay_info.get('amount', 0.0),
                                'payment_id': pay_info.get('account_payment_id', False), # Puede ser False si es asiento manual
                                'ref': pay_info.get('ref', 'Asiento Manual/Vario')
                            })
                except Exception as e:
                    _logger.error(f"[COMMISSION] Error leyendo widget: {e}")

            # ---------------------------------------------------------
            # MÉTODO 2: Conciliaciones (Plan B si falla el widget)
            # ---------------------------------------------------------
            if not payments_found:
                _logger.info("[COMMISSION] Widget vacío. Buscando conciliaciones físicas...")
                # Buscamos en TODAS las líneas de la factura que tengan conciliaciones,
                # no nos limitamos por tipo de cuenta, por si acaso.
                for line in inv.line_ids:
                    partials = line.matched_credit_ids | line.matched_debit_ids
                    if not partials:
                        continue
                        
                    for partial in partials:
                        # Para evitar duplicados si la conciliación aparece en ambos lados
                        # verificamos que 'line' sea la parte de la factura
                        if partial.amount <= 0:
                            continue

                        # Intentar buscar el ID del pago si existe
                        counterpart = partial.credit_move_id if partial.debit_move_id == line else partial.debit_move_id
                        pay_obj = self.env['account.payment'].search([('move_id', '=', counterpart.move_id.id)], limit=1)
                        
                        payments_found.append({
                            'amount': partial.amount,
                            'payment_id': pay_obj.id if pay_obj else False,
                            'ref': counterpart.move_id.name
                        })

            if not payments_found:
                msg = f"Factura {inv.name}: Odoo dice '{inv.payment_state}' pero no hallamos el monto."
                _logger.warning(f"[COMMISSION] {msg}")
                debug_logs.append(msg)
                continue

            # ---------------------------------------------------------
            # GENERACIÓN
            # ---------------------------------------------------------
            is_refund = (inv.move_type == 'out_refund')
            sign = -1 if is_refund else 1

            for pay_data in payments_found:
                amount_paid = pay_data['amount']
                payment_id = pay_data['payment_id']
                
                # Protección contra montos cero
                if amount_paid <= 0.01: 
                    continue

                if inv.amount_total == 0:
                    continue

                # Calculamos el % que representa este pago sobre el total de la factura
                ratio = amount_paid / inv.amount_total
                amount_paid_signed = amount_paid * sign

                for rule in self.commission_rule_ids:
                    # Candado: Buscamos si ya pagamos comisión a este partner por este monto aproximado
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('base_amount_paid', '>=', amount_paid_signed - 0.02),
                        ('base_amount_paid', '<=', amount_paid_signed + 0.02),
                    ]
                    
                    # Si tenemos ID de pago, lo usamos en el filtro. 
                    # Si no (es asiento manual), confiamos en el monto y el partner.
                    if payment_id:
                        domain.append(('payment_id', '=', payment_id))
                    else:
                        domain.append(('payment_id', '=', False))
                        
                    existing = CommissionMove.search(domain)
                    if existing:
                        _logger.info(f"[COMMISSION] Ya existe comisión para {rule.partner_id.name} (Monto base: {amount_paid})")
                        continue

                    # Cálculo del dinero
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
                            'payment_id': payment_id or False, # Guardamos False si no hay objeto payment
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

        # Retorno a interfaz
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