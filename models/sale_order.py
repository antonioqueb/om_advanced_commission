from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    no_commission = fields.Boolean(string='Excluir de Comisión', help='Si se marca, esta línea no suma a la base de cálculo.')
