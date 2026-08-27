from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    commission_product_id = fields.Many2one(
        'product.product',
        string='Producto para Comisiones',
        config_parameter='om_advanced_commission.default_commission_product_id',
        help='Producto de servicio usado al generar facturas de proveedor para comisionistas.'
    )
    commission_journal_id = fields.Many2one(
        'account.journal',
        string='Diario de Comisiones',
        config_parameter='om_advanced_commission.default_commission_journal_id',
        domain=[('type', '=', 'purchase')]
    )
    commission_seller_max_percent = fields.Float(
        string='% Máx. Vendedores sin Autorización',
        config_parameter='om_advanced_commission.seller_max_percent',
        default=2.5,
        help='Tope del total de vendedores internos por orden. Aplica a órdenes nuevas; '
             'las existentes conservan su permitido.')
    commission_external_max_percent = fields.Float(
        string='% Máx. Externo sin Autorización',
        config_parameter='om_advanced_commission.external_max_percent',
        default=0.0,
        help='Tope de comisiones externas (embajador/constructora/referidor) como % del subtotal. 0 = sin tope.')
