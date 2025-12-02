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
