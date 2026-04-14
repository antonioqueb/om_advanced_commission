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
        ('gross_utility', 'Utilidad Bruta (Subtotal - Comisiones Externas)'),
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
                 'sale_order_id.order_line.price_subtotal',
                 'sale_order_id.commission_rule_ids.percent',
                 'sale_order_id.commission_rule_ids.fixed_amount',
                 'sale_order_id.commission_rule_ids.calculation_base',
                 'sale_order_id.commission_rule_ids.role_type')
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

            elif rule.calculation_base == 'gross_utility':
                # Utilidad bruta = Subtotal - todas las comisiones externas
                # (arquitectos, constructoras, referidores, y otros vendedores internos)
                subtotal = sum(lines.mapped('price_subtotal'))

                # Sumar comisiones de roles NO internos (arquitecto, constructora, referidor)
                external_commission = 0.0
                for other in so.commission_rule_ids:
                    if other.id == rule.id:
                        continue
                    if other.role_type != 'internal':
                        if other.calculation_base == 'manual':
                            external_commission += other.fixed_amount
                        elif other.calculation_base in ('amount_untaxed', 'gross_utility'):
                            external_commission += subtotal * (other.percent / 100.0)
                        elif other.calculation_base == 'amount_total':
                            external_commission += sum(lines.mapped('price_total')) * (other.percent / 100.0)

                gross_utility = subtotal - external_commission
                amount = gross_utility * (rule.percent / 100.0)

            rule.estimated_amount = amount