from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    commission_payout_method = fields.Selection([
        ('vendor_bill', 'Factura de proveedor'),
        ('payroll', 'Nómina'),
        ('none', 'Sin documento (solo referencia)'),
    ], string='Pago de Comisiones', default='vendor_bill',
        help='Cómo se liquida a este beneficiario. Vendedores internos (empleados) normalmente por nómina; '
             'externos por factura de proveedor (las retenciones ISR/IVA vienen de los impuestos de proveedor del producto).')
    commission_product_id = fields.Many2one(
        'product.product', string='Producto de Comisión',
        domain=[('type', '=', 'service')],
        help='Producto usado en la factura de proveedor de este beneficiario. '
             'Vacío = el producto general de Ajustes. Útil para persona física con retenciones.')
