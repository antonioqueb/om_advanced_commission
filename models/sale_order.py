from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')
    x_project_id = fields.Many2one('project.project', string='Proyecto (Job Name)')

    def action_recalc_commissions(self):
        """
        Recálculo forzado usando lógica de Asiento Contable (MXN)
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        debug_logs = []
        
        company_currency = self.company_id.currency_id

        _logger.info(f"[COMMISSION] --- Inicio recálculo MXN para: {self.name} ---")

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            return self._return_notification("Sin facturas válidas.", "warning")

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        for inv in invoices:
            if inv.payment_state == 'not_paid':
                continue
            
            # --- 1. Obtener valores del Asiento Contable (MXN) ---
            invoice_total_mxn = abs(inv.amount_total_signed)
            
            if invoice_total_mxn == 0:
                continue

            # --- 2. Buscar Pagos (En moneda original de la factura) ---
            payments_found = []
            
            # A. Widget
            if inv.invoice_payments_widget:
                try:
                    data = inv.invoice_payments_widget
                    content = data.get('content', []) if isinstance(data, dict) else []
                    for pay_info in content:
                        payments_found.append({
                            'amount_currency': pay_info.get('amount', 0.0), # Moneda Factura
                            'payment_id': pay_info.get('account_payment_id', False),
                            'ref': pay_info.get('ref', 'Vía Widget')
                        })
                except Exception: pass

            # B. Reconciliaciones
            if not payments_found:
                invoice_move_lines = inv.line_ids
                partials = self.env['account.partial.reconcile'].search([
                    '|', ('debit_move_id', 'in', invoice_move_lines.ids),
                    ('credit_move_id', 'in', invoice_move_lines.ids)
                ])
                for partial in partials:
                    payments_found.append({
                        'amount_currency': partial.amount, 
                        'payment_id': False, 
                        'ref': f"Conciliación {partial.id}"
                    })

            # C. Fallback Matemático
            if not payments_found and inv.amount_total > 0:
                paid_amount = inv.amount_total - inv.amount_residual
                if paid_amount > 0.01:
                    payments_found.append({'amount_currency': paid_amount, 'payment_id': False, 'ref': 'Saldo Calculado'})
                elif inv.payment_state in ['in_payment', 'paid']:
                    payments_found.append({'amount_currency': inv.amount_total, 'payment_id': False, 'ref': 'Forzado Estado'})

            if not payments_found:
                continue

            # --- 3. Cálculo ---
            is_refund = (inv.move_type == 'out_refund')
            sign = -1 if is_refund else 1

            # Convertir SO Total a MXN para el denominador
            so_total_mxn = self.currency_id._convert(
                self.amount_total,
                company_currency,
                self.company_id,
                self.date_order or fields.Date.today()
            )

            for pay_data in payments_found:
                amount_paid_currency = pay_data['amount_currency']
                
                if amount_paid_currency <= 0.01 or inv.amount_total == 0: continue

                # A. Ratio de cobertura (Moneda Factura / Moneda Factura) -> Unitless
                payment_ratio = amount_paid_currency / inv.amount_total
                
                # B. Valor en MXN pagado (Basado en el asiento contable)
                paid_mxn_real = invoice_total_mxn * payment_ratio
                
                # C. Ratio final contra la Venta en MXN
                final_ratio = paid_mxn_real / so_total_mxn if so_total_mxn else 0
                
                amount_paid_signed_mxn = paid_mxn_real * sign

                for rule in self.commission_rule_ids:
                    # Candado Anti-Duplicados (En MXN)
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('currency_id', '=', company_currency.id), # Check duplicados en MXN
                        ('base_amount_paid', '>=', amount_paid_signed_mxn - 1.0), # Tolerancia de $1 peso por redondeos
                        ('base_amount_paid', '<=', amount_paid_signed_mxn + 1.0),
                    ]
                    
                    if CommissionMove.search_count(domain) > 0:
                        continue

                    # Calcular Comisión en MXN
                    comm_amount_mxn = 0.0
                    
                    # Convertir estimados a MXN
                    rule_estimated_mxn = self.currency_id._convert(
                        rule.estimated_amount,
                        company_currency,
                        self.company_id,
                        self.date_order or fields.Date.today()
                    )
                    
                    rule_fixed_mxn = rule.currency_id._convert(
                        rule.fixed_amount,
                        company_currency,
                        self.company_id,
                        self.date_order or fields.Date.today()
                    ) if rule.fixed_amount else 0.0

                    if rule.calculation_base == 'manual':
                        comm_amount_mxn = rule_fixed_mxn * final_ratio
                    else:
                        comm_amount_mxn = rule_estimated_mxn * final_ratio
                    
                    comm_amount_mxn *= sign

                    if abs(comm_amount_mxn) > 0.01:
                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': self.id,
                            'payment_id': pay_data['payment_id'] or False,
                            'invoice_line_id': inv.invoice_line_ids[0].id if inv.invoice_line_ids else False,
                            'amount': comm_amount_mxn,
                            'base_amount_paid': amount_paid_signed_mxn,
                            'currency_id': company_currency.id, # GUARDAR EN MXN
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {inv.name} ({pay_data['ref']})"
                        })
                        created_count += 1
                        debug_logs.append(f"+ {comm_amount_mxn} {company_currency.symbol}")

        msg_type = "success" if created_count > 0 else "info"
        return self._return_notification(f"Finalizado MXN. {created_count} creadas.", msg_type)

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