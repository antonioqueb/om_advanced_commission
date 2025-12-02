from odoo import models, api, fields

class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        """ 
        Detecta pagos sobre facturas de venta y calcula comisión proporcional 
        considerando reglas de la OV original.
        """
        CommissionMove = self.env['commission.move']
        
        for rec in self:
            # Identificar factura (debit) y pago (credit) o viceversa para Notas Crédito
            invoice = rec.debit_move_id.move_id
            payment = rec.credit_move_id.move_id
            amount_reconciled = rec.amount
            is_refund = False

            # Lógica para detectar Nota de Crédito (Clawback)
            if invoice.move_type not in ['out_invoice', 'out_refund']:
                # Intentar invertir
                invoice = rec.credit_move_id.move_id
                payment = rec.debit_move_id.move_id
            
            if invoice.move_type == 'out_refund':
                is_refund = True
                # Buscar factura original si existe, sino usar la NC
                invoice_origin = invoice.reversed_entry_id or invoice
            else:
                invoice_origin = invoice

            if invoice_origin.move_type not in ['out_invoice', 'out_refund']:
                continue

            # Buscar Ventas relacionadas
            sale_orders = invoice_origin.invoice_line_ids.mapped('sale_line_ids.order_id')
            
            for so in sale_orders:
                if not so.commission_rule_ids:
                    continue

                # Factor de Proporción: (Monto Conciliado / Total Factura)
                # Nota: Usamos invoice_origin.amount_total para consistencia
                if invoice_origin.amount_total == 0:
                    continue
                    
                ratio = amount_reconciled / invoice_origin.amount_total
                sign = -1 if is_refund else 1

                for rule in so.commission_rule_ids:
                    commission_amount = 0.0
                    
                    # 1. Base Manual / Fija
                    if rule.calculation_base == 'manual':
                        # Si es fijo, prorrateamos el monto fijo total por el % pagado
                        commission_amount = rule.fixed_amount * ratio
                    
                    # 2. Bases Calculadas (Total, Untaxed, Margin)
                    else:
                        # Necesitamos saber qué líneas de la factura corresponden a líneas "comisionables" de la SO
                        # Esto es complejo si la factura es parcial. 
                        # Simplificación robusta: Usar el estimado de la regla y aplicar ratio.
                        commission_amount = rule.estimated_amount * ratio

                    if commission_amount != 0:
                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': so.id,
                            'payment_id': self.env['account.payment'].search([('move_id', '=', payment.id)], limit=1).id,
                            'amount': commission_amount * sign,
                            'base_amount_paid': amount_reconciled * sign,
                            'currency_id': so.currency_id.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {invoice.name} ({round(ratio*100, 1)}%)"
                        })

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        # Disparar cálculo post-conciliación
        res._create_commission_moves()
        return res
