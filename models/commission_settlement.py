from odoo import models, fields, api

class CommissionSettlement(models.Model):
    _name = 'commission.settlement'
    _description = 'Hoja de Liquidación de Comisiones'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Referencia', default='Borrador', copy=False)
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)
    date = fields.Date(string='Fecha Corte', default=fields.Date.context_today)
    
    move_ids = fields.One2many('commission.move', 'settlement_id', string='Movimientos')
    
    total_amount = fields.Monetary(compute='_compute_totals', string='Total a Pagar', store=True)
    
    # CORRECCIÓN: Se añade default para evitar error NOT NULL en creación manual
    currency_id = fields.Many2one('res.currency', required=True, default=lambda self: self.env.company.currency_id)
    
    vendor_bill_id = fields.Many2one('account.move', string='Factura Proveedor Generada')
    
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('approved', 'Aprobado'),
        ('invoiced', 'Facturado'),
        ('cancel', 'Cancelado')
    ], default='draft', tracking=True)

    @api.depends('move_ids.amount')
    def _compute_totals(self):
        for rec in self:
            rec.total_amount = sum(rec.move_ids.mapped('amount'))

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_create_bill(self):
        self.ensure_one()
        # Obtener parametros de sistema de manera segura
        param_obj = self.env['ir.config_parameter'].sudo()
        prod_id_str = param_obj.get_param('om_advanced_commission.default_commission_product_id')
        journal_id_str = param_obj.get_param('om_advanced_commission.default_commission_journal_id')

        if not prod_id_str or not journal_id_str:
            raise models.ValidationError("Falta configuración. Ve a Ajustes > Ventas > Configuración Comisiones.")

        product_id = int(prod_id_str)
        journal_id = int(journal_id_str)

        invoice_vals = {
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id,
            'invoice_date': fields.Date.today(),
            'journal_id': journal_id,
            'currency_id': self.currency_id.id,
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': product_id,
                    'name': f"Liquidación Comisiones Ref: {self.name}",
                    'quantity': 1,
                    'price_unit': self.total_amount,
                })
            ]
        }
        bill = self.env['account.move'].create(invoice_vals)
        self.vendor_bill_id = bill.id
        self.state = 'invoiced'
        self.move_ids.write({'state': 'invoiced'})
        
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': bill.id,
            'view_mode': 'form',
        }