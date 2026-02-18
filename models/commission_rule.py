from odoo import models, fields, api
from odoo.exceptions import ValidationError


class SaleCommissionRule(models.Model):
    _name = 'sale.commission.rule'
    _description = 'Regla de Comisión en Ventas'

    sale_order_id = fields.Many2one('sale.order', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)

    role_type = fields.Selection([
        ('internal', 'Vendedor'),
        ('architect', 'Arquitecto'),
        ('construction', 'Constructora'),
        ('referrer', 'Referidor')
    ], string='Rol', required=True, default='internal')

    calculation_base = fields.Selection([
        ('amount_untaxed', 'Monto Base (Subtotal)'),
        ('amount_total', 'Monto Total (Inc. Impuestos)'),
        ('margin', 'Margen (Ganancia)'),
        ('manual', 'Manual / Fijo')
    ], string='Base de Cálculo', default='amount_untaxed', required=True)

    percent = fields.Float(string='Porcentaje %')
    fixed_amount = fields.Monetary(string='Monto Fijo', currency_field='currency_id')

    estimated_amount = fields.Monetary(compute='_compute_estimated', string='Estimado Total')
    currency_id = fields.Many2one(related='sale_order_id.currency_id')

    requires_authorization = fields.Boolean(string='Requiere Autorización', default=False, readonly=True)
    authorization_id = fields.Many2one('commission.authorization', string='Autorización', readonly=True)

    @api.depends('percent', 'fixed_amount', 'calculation_base',
                 'sale_order_id.amount_untaxed', 'sale_order_id.amount_total',
                 'sale_order_id.order_line.price_subtotal')
    def _compute_estimated(self):
        for rule in self:
            so = rule.sale_order_id
            amount = 0.0
            lines = so.order_line.filtered(lambda l: not getattr(l, 'no_commission', False))

            if rule.calculation_base == 'manual':
                amount = rule.fixed_amount
            elif rule.calculation_base == 'amount_untaxed':
                base = sum(lines.mapped('price_subtotal'))
                amount = base * (rule.percent / 100.0)
            elif rule.calculation_base == 'amount_total':
                base = sum(lines.mapped('price_total'))
                amount = base * (rule.percent / 100.0)
            elif rule.calculation_base == 'margin':
                base = 0.0
                try:
                    if lines and 'margin' in lines[0]._fields:
                        base = sum(lines.mapped('margin'))
                    elif 'margin' in so._fields:
                        base = so.margin
                except (AttributeError, KeyError):
                    base = 0.0
                amount = base * (rule.percent / 100.0)

            rule.estimated_amount = amount