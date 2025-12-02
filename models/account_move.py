from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)

class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        """ 
        Detecta pagos sobre facturas de venta y calcula comisión proporcional.
        Incluye LOGGING para depuración en Odoo 19.
        """
        CommissionMove = self.env['commission.move']
        
        for rec in self:
            try:
                # 1. Identificar factura (debit) y pago (credit)
                invoice = rec.debit_move_id.move_id
                payment = rec.credit_move_id.move_id
                amount_reconciled = rec.amount
                is_refund = False

                # Caso inverso (Devoluciones o configuración contable opuesta)
                if invoice.move_type not in ['out_invoice', 'out_refund']:
                    invoice = rec.credit_move_id.move_id
                    payment = rec.debit_move_id.move_id
                
                # _logger.info(f"[COMMISSION] Procesando conciliación: {amount_reconciled} para Doc: {invoice.name} Tipo: {invoice.move_type}")

                if invoice.move_type == 'out_refund':
                    is_refund = True
                    invoice_origin = invoice.reversed_entry_id or invoice
                else:
                    invoice_origin = invoice

                if invoice_origin.move_type not in ['out_invoice', 'out_refund']:
                    # _logger.info(f"[COMMISSION] Ignorado: El documento {invoice_origin.name} no es factura de cliente.")
                    continue

                # 2. Buscar Ventas relacionadas (La parte crítica)
                # Intentamos obtener las lineas de venta vinculadas a las lineas de factura
                sale_line_ids = invoice_origin.invoice_line_ids.mapped('sale_line_ids')
                sale_orders = sale_line_ids.mapped('order_id')
                
                if not sale_orders:
                    # Intento de rescate: Buscar por el campo 'invoice_ids' en sale.order si existe la relación inversa
                    # Esto ayuda en casos de anticipos complejos
                    sale_orders = self.env['sale.order'].search([('invoice_ids', 'in', invoice_origin.id)])
                    if not sale_orders:
                        _logger.warning(f"[COMMISSION] ALERTA: Factura {invoice_origin.name} pagada, pero no se encontró Orden de Venta vinculada.")
                        continue

                for so in sale_orders:
                    if not so.commission_rule_ids:
                        _logger.info(f"[COMMISSION] Venta {so.name} encontrada, pero no tiene reglas de comisión.")
                        continue

                    # 3. Calcular Ratio
                    if invoice_origin.amount_total == 0:
                        continue
                        
                    ratio = amount_reconciled / invoice_origin.amount_total
                    sign = -1 if is_refund else 1
                    
                    _logger.info(f"[COMMISSION] Calculando: Venta {so.name} | Pago: {amount_reconciled} | Ratio: {ratio}")

                    for rule in so.commission_rule_ids:
                        commission_amount = 0.0
                        
                        # A. Base Manual / Fija
                        if rule.calculation_base == 'manual':
                            commission_amount = rule.fixed_amount * ratio
                        
                        # B. Bases Calculadas
                        else:
                            commission_amount = rule.estimated_amount * ratio

                        if commission_amount != 0:
                            CommissionMove.create({
                                'partner_id': rule.partner_id.id,
                                'sale_order_id': so.id,
                                'invoice_line_id': invoice_origin.invoice_line_ids[0].id if invoice_origin.invoice_line_ids else False,
                                'payment_id': self.env['account.payment'].search([('move_id', '=', payment.id)], limit=1).id,
                                'amount': commission_amount * sign,
                                'base_amount_paid': amount_reconciled * sign,
                                'currency_id': so.currency_id.id,
                                'is_refund': is_refund,
                                'state': 'draft',
                                'name': f"Cmsn: {invoice.name} ({round(ratio*100, 1)}%)"
                            })
                            _logger.info(f"[COMMISSION] GENERADA: {commission_amount} para {rule.partner_id.name}")

            except Exception as e:
                _logger.error(f"[COMMISSION] Error crítico al procesar conciliación ID {rec.id}: {str(e)}")

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        # Disparar cálculo post-conciliación
        res._create_commission_moves()
        return res