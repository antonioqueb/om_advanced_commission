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
    commission_base_on_cash_received = fields.Boolean(
        string='Comisión sobre pesos efectivamente recibidos',
        config_parameter='om_advanced_commission.base_on_cash_received',
        default=True,
        help='Facturas en divisa: la base son los pesos que entraron al tipo de cambio del pago. '
             'Desactivado = pesos facturados al tipo de cambio de la factura.')
    commission_incident_abs_tolerance = fields.Float(
        string='Tolerancia de saldo a favor (monto)',
        config_parameter='om_advanced_commission.incident_abs_tolerance', default=1000.0,
        help='Saldo a favor permitido sin incidencia, en moneda de la compañía.')
    commission_incident_pct_tolerance = fields.Float(
        string='Tolerancia de saldo a favor (%)',
        config_parameter='om_advanced_commission.incident_pct_tolerance', default=10.0,
        help='Porcentaje sobre lo adeudado/aplicado. Se usa el mayor entre monto y porcentaje.')
    commission_external_max_percent = fields.Float(
        string='% Máx. Externo sin Autorización',
        config_parameter='om_advanced_commission.external_max_percent',
        default=0.0,
        help='Tope de comisiones externas (embajador/constructora/referidor) como % del subtotal. 0 = sin tope.')
