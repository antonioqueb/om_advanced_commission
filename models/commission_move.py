from odoo import models, fields, api

class CommissionMove(models.Model):
    _name = 'commission.move'
    _description = 'Movimiento Individual de Comisión'
    _order = 'date desc, id desc'

    name = fields.Char(string='Referencia', required=True, default='/')
    
    # Relaciones
    partner_id = fields.Many2one('res.partner', string='Comisionista', required=True, index=True)
    sale_order_id = fields.Many2one('sale.order', string='Origen Venta')
    invoice_line_id = fields.Many2one('account.move.line', string='Línea de Factura Origen') # Para tracking preciso
    payment_id = fields.Many2one('account.payment', string='Pago Cliente')
    
    settlement_id = fields.Many2one('commission.settlement', string='Liquidación', ondelete='set null')

    # Datos Económicos
    amount = fields.Monetary(string='Monto Comisión', currency_field='currency_id')
    base_amount_paid = fields.Monetary(string='Base Cobrada', help='Monto del pago cliente que generó esta comisión')
    currency_id = fields.Many2one('res.currency', required=True)
    
    date = fields.Date(default=fields.Date.context_today)
    
    # Estado
    is_refund = fields.Boolean(string='Es Devolución', default=False)
    state = fields.Selection([
        ('draft', 'Pendiente'),
        ('settled', 'En Liquidación'),
        ('invoiced', 'Facturado/Pagado'),
        ('cancel', 'Cancelado')
    ], default='draft', string='Estado', index=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('commission.move') or 'COMM'
        return super().create(vals_list)
