from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')
    
    # Nuevo campo requerido para el reporte (Job Name)
    x_project_id = fields.Many2one('project.project', string='Proyecto (Job Name)')

    def action_recalc_commissions(self):
        """
        Botón de emergencia/manual para generar comisiones.
        Versión: CORREGIDA (Calcula proporción sobre la Venta Original, no sobre la Factura).
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

        # Limpiar comisiones previas en borrador para evitar duplicados sucios (Opcional)
        # self.env['commission.move'].search([('sale_order_id', '=', self.id), ('state', '=', 'draft')]).unlink()

        for inv in invoices:
            if inv.payment_state == 'not_paid':
                continue
            
            # ---------------------------------------------------------
            # ESTRATEGIA DE BÚSQUEDA DE PAGOS
            # ---------------------------------------------------------
            payments_found = []
            
            # 1. Widget de Pagos (JSON)
            if inv.invoice_payments_widget:
                try:
                    data = inv.invoice_payments_widget
                    content = data.get('content', []) if isinstance(data, dict) else []
                    for pay_info in content:
                        payments_found.append({
                            'amount': pay_info.get('amount', 0.0),
                            'payment_id': pay_info.get('account_payment_id', False),
                            'ref': pay_info.get('ref', 'Vía Widget')
                        })
                except Exception: 
                    pass

            # 2. Búsqueda Física (Conciliaciones Parciales)
            if not payments_found:
                invoice_move_lines = inv.line_ids
                partials = self.env['account.partial.reconcile'].search([
                    '|', ('debit_move_id', 'in', invoice_move_lines.ids),
                    ('credit_move_id', 'in', invoice_move_lines.ids)
                ])
                for partial in partials:
                    payments_found.append({
                        'amount': partial.amount, 
                        'payment_id': False, 
                        'ref': f"Conciliación {partial.id}"
                    })

            # 3. Matemático / Forzado (Red de seguridad)
            if not payments_found and inv.amount_total > 0:
                paid_amount = inv.amount_total - inv.amount_residual
                if paid_amount > 0.01:
                    payments_found.append({
                        'amount': paid_amount, 
                        'payment_id': False, 
                        'ref': 'Saldo Calculado'
                    })
                elif inv.payment_state in ['in_payment', 'paid']:
                    payments_found.append({
                        'amount': inv.amount_total, 
                        'payment_id': False, 
                        'ref': 'Forzado Estado Visual'
                    })

            if not payments_found:
                continue

            # ---------------------------------------------------------
            # CÁLCULO Y GENERACIÓN
            # ---------------------------------------------------------
            is_refund = (inv.move_type == 'out_refund')
            sign = -1 if is_refund else 1

            # Calcular cuánto de esta factura pertenece realmente a ESTA venta
            # (Útil para facturas agrupadas o anticipos)
            relevant_lines = inv.invoice_line_ids.filtered(lambda l: self.id in l.sale_line_ids.order_id.ids)
            so_inv_total = sum(relevant_lines.mapped('price_total')) if relevant_lines else inv.amount_total

            for pay_data in payments_found:
                amount_paid_on_invoice = pay_data['amount']
                payment_id = pay_data['payment_id']
                
                # Validaciones básicas
                if amount_paid_on_invoice <= 0.01 or inv.amount_total == 0 or self.amount_total == 0: 
                    continue

                # --- LÓGICA MATEMÁTICA CORREGIDA ---
                # 1. ¿Qué porcentaje de la factura cubrió este pago? (Ej. Factura de 800, Pago de 800 = 100%)
                invoice_payment_ratio = amount_paid_on_invoice / inv.amount_total
                
                # 2. ¿Cuánto dinero de la VENTA representa eso? (Ej. 800 * 100% = 800)
                paid_amount_so_real = so_inv_total * invoice_payment_ratio
                
                # 3. ¿Qué porcentaje de la VENTA TOTAL es eso? (Ej. 800 / 10,000 = 0.08)
                final_ratio = paid_amount_so_real / self.amount_total
                
                amount_paid_signed = paid_amount_so_real * sign

                for rule in self.commission_rule_ids:
                    # Candado Anti-Duplicados (Usando la base calculada real)
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('base_amount_paid', '>=', amount_paid_signed - 0.1),
                        ('base_amount_paid', '<=', amount_paid_signed + 0.1),
                    ]
                    if payment_id:
                        domain.append(('payment_id', '=', payment_id))
                    
                    if CommissionMove.search_count(domain) > 0:
                        _logger.info(f"[COMMISSION] Duplicado evitado para {rule.partner_id.name}")
                        continue

                    # Cálculo del monto de comisión
                    comm_amount = 0.0
                    if rule.calculation_base == 'manual':
                        comm_amount = rule.fixed_amount * final_ratio
                    else:
                        comm_amount = rule.estimated_amount * final_ratio
                    
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
        final_msg = f"Finalizado. {created_count} comisiones generadas."
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