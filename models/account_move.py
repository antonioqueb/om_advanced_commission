from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)

class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        """ 
        Detecta pagos y calcula comisión basada SIEMPRE en la moneda de la compañía (MXN),
        usando los valores del asiento contable (amount_total_signed) para evitar
        problemas con facturas en dólares.
        """
        CommissionMove = self.env['commission.move']
        
        for rec in self:
            try:
                # 1. Identificar factura (debit) y pago (credit) de forma segura
                invoice = rec.debit_move_id.move_id
                payment = rec.credit_move_id.move_id
                amount_reconciled_currency_invoice = rec.amount # Monto en moneda de la factura/pago
                is_refund = False

                if invoice.move_type not in ['out_invoice', 'out_refund']:
                    invoice = rec.credit_move_id.move_id
                    payment = rec.debit_move_id.move_id
                
                if invoice.move_type == 'out_refund':
                    is_refund = True
                    invoice_origin = invoice.reversed_entry_id or invoice
                else:
                    invoice_origin = invoice

                if invoice_origin.move_type not in ['out_invoice', 'out_refund']:
                    continue

                # 2. Buscar Ventas relacionadas
                sale_line_ids = invoice_origin.invoice_line_ids.mapped('sale_line_ids')
                sale_orders = sale_line_ids.mapped('order_id')
                
                if not sale_orders:
                    sale_orders = self.env['sale.order'].search([('invoice_ids', 'in', invoice_origin.id)])
                    if not sale_orders:
                        continue

                # 3. Preparar datos en Moneda de la Compañía (MXN)
                company = invoice_origin.company_id
                company_currency = company.currency_id
                
                # Obtener el valor contable real en MXN de la factura (Asiento Contable)
                # Usamos abs() porque signed puede ser negativo dependiendo de la naturaleza contable
                invoice_total_mxn = abs(invoice_origin.amount_total_signed)
                
                # Evitar división por cero
                if invoice_origin.amount_total == 0 or invoice_total_mxn == 0:
                    continue

                # Calcular qué porcentaje de la factura en moneda original se está pagando
                # Ej: Factura 100 USD, Pago 50 USD -> Ratio 0.5
                payment_ratio = amount_reconciled_currency_invoice / invoice_origin.amount_total

                # Calcular cuánto representa ese pago en MXN según el asiento de la factura
                # Ej: Factura 2000 MXN * 0.5 = 1000 MXN pagados
                paid_amount_mxn = invoice_total_mxn * payment_ratio

                for so in sale_orders:
                    if not so.commission_rule_ids:
                        continue
                    
                    # Convertir el total de la SO a MXN (para mantener la proporción correcta de anticipos)
                    # Usamos la fecha de la orden para la conversión histórica o el día de hoy
                    so_total_mxn = so.currency_id._convert(
                        so.amount_total,
                        company_currency,
                        so.company_id,
                        so.date_order or fields.Date.today()
                    )

                    if so_total_mxn == 0:
                        continue

                    # Calcular proporción sobre la venta total en MXN
                    final_ratio_mxn = paid_amount_mxn / so_total_mxn
                    sign = -1 if is_refund else 1

                    _logger.info(f"[COMMISSION] Factura: {invoice.name} | Pago MXN: {paid_amount_mxn} | Ratio MXN: {final_ratio_mxn}")

                    for rule in so.commission_rule_ids:
                        commission_amount_mxn = 0.0
                        
                        # Convertir el estimado de la regla a MXN
                        rule_estimated_mxn = so.currency_id._convert(
                            rule.estimated_amount,
                            company_currency,
                            so.company_id,
                            so.date_order or fields.Date.today()
                        )
                        
                        # Convertir monto fijo manual a MXN si fuera necesario
                        rule_fixed_mxn = 0.0
                        if rule.calculation_base == 'manual':
                            rule_fixed_mxn = rule.currency_id._convert(
                                rule.fixed_amount,
                                company_currency,
                                so.company_id,
                                so.date_order or fields.Date.today()
                            )

                        # Cálculo final en MXN
                        if rule.calculation_base == 'manual':
                            commission_amount_mxn = rule_fixed_mxn * final_ratio_mxn
                        else:
                            commission_amount_mxn = rule_estimated_mxn * final_ratio_mxn

                        if abs(commission_amount_mxn) > 0.01:
                            CommissionMove.create({
                                'partner_id': rule.partner_id.id,
                                'sale_order_id': so.id,
                                'invoice_line_id': invoice_origin.invoice_line_ids[0].id if invoice_origin.invoice_line_ids else False,
                                'payment_id': self.env['account.payment'].search([('move_id', '=', payment.id)], limit=1).id,
                                'amount': commission_amount_mxn * sign,
                                'base_amount_paid': paid_amount_mxn * sign,
                                'currency_id': company_currency.id, # ¡Forzamos MXN!
                                'is_refund': is_refund,
                                'state': 'draft',
                                'name': f"Cmsn: {invoice.name} ({round(final_ratio_mxn*100, 1)}%)"
                            })

            except Exception as e:
                _logger.error(f"[COMMISSION] Error crítico ID {rec.id}: {str(e)}")

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        res._create_commission_moves()
        return res