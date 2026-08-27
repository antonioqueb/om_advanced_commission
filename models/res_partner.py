from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    commission_move_ids = fields.One2many('commission.move', 'partner_id', string='Movimientos de Comisión')

    @api.model
    def _commission_beneficiary_domain(self):
        """Quién puede aparecer en selectores de comisionista: usuarios de
        Odoo con permisos de ventas, o contactos que YA tienen comisiones
        (beneficiarios externos). Nunca la agenda completa."""
        seller = self.env['sale.order']._commission_seller_domain()
        return ['|', ('commission_move_ids', '!=', False)] + ['&'] * (len(seller) - 1) + seller

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
