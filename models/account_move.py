from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)

class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        """ 
        Detecta pagos sobre facturas de venta y calcula comisión proporcional
        basada en el porcentaje real pagado sobre el TOTAL DE LA VENTA (SO),
        no sobre el total de la factura.
        """
        CommissionMove = self.env['commission.move']
        
        for rec in self:
            try:
                # 1. Identificar factura (debit) y pago (credit)
                invoice = rec.debit_move_id.move_id
                payment = rec.credit_move_id.move_id
                amount_reconciled = rec.amount
                is_refund = False

                # Caso inverso
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
                        _logger.warning(f"[COMMISSION] ALERTA: Factura {invoice_origin.name} pagada, sin Orden de Venta.")
                        continue

                for so in sale_orders:
                    if not so.commission_rule_ids:
                        continue

                    # --- CORRECCIÓN MATEMÁTICA INICIO ---
                    if invoice_origin.amount_total == 0 or so.amount_total == 0:
                        continue
                    
                    # A. ¿Cuánto de esta factura pertenece a ESTA venta específica?
                    # (Vital si agrupas varias ventas en una factura, o para anticipos)
                    relevant_lines = invoice_origin.invoice_line_ids.filtered(
                        lambda l: so.id in l.sale_line_ids.order_id.ids
                    )
                    
                    # Si no encontramos líneas directas (caso raro de migración), asumimos el total
                    so_invoice_total = sum(relevant_lines.mapped('price_total')) if relevant_lines else invoice_origin.amount_total

                    # B. ¿Qué porcentaje de la FACTURA se está pagando ahora?
                    payment_coverage_ratio = amount_reconciled / invoice_origin.amount_total

                    # C. ¿Cuánto dinero REAL de la VENTA se está pagando?
                    paid_amount_attributable_to_so = so_invoice_total * payment_coverage_ratio

                    # D. RATIO FINAL: (Monto de Venta Pagado) / (Total Venta Original)
                    # Esto normaliza el anticipo (ej. 800 pagados / 1000 venta = 0.8)
                    ratio = paid_amount_attributable_to_so / so.amount_total
                    
                    # --- CORRECCIÓN MATEMÁTICA FIN ---

                    sign = -1 if is_refund else 1
                    
                    _logger.info(f"[COMMISSION] Venta {so.name} | Pagado SO: {paid_amount_attributable_to_so} | Ratio Final: {ratio}")

                    for rule in so.commission_rule_ids:
                        commission_amount = 0.0
                        
                        # Al usar el nuevo ratio contra el 'estimated_amount' (que es el total teórico),
                        # obtenemos la porción correcta.
                        if rule.calculation_base == 'manual':
                            commission_amount = rule.fixed_amount * ratio
                        else:
                            commission_amount = rule.estimated_amount * ratio

                        if abs(commission_amount) > 0.001: # Filtro de redondeo
                            CommissionMove.create({
                                'partner_id': rule.partner_id.id,
                                'sale_order_id': so.id,
                                'invoice_line_id': invoice_origin.invoice_line_ids[0].id if invoice_origin.invoice_line_ids else False,
                                'payment_id': self.env['account.payment'].search([('move_id', '=', payment.id)], limit=1).id,
                                'amount': commission_amount * sign,
                                'base_amount_paid': paid_amount_attributable_to_so * sign, # Guardamos la base real de venta
                                'currency_id': so.currency_id.id,
                                'is_refund': is_refund,
                                'state': 'draft',
                                'name': f"Cmsn: {invoice.name} ({round(ratio*100, 1)}%)"
                            })

            except Exception as e:
                _logger.error(f"[COMMISSION] Error crítico ID {rec.id}: {str(e)}")

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        res._create_commission_moves()
        return res