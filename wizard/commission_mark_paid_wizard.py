from odoo import models, fields, api


class CommissionMarkPaidWizard(models.TransientModel):
    _name = 'commission.mark.paid.wizard'
    _description = 'Marcar comisiones como cobradas fuera del sistema'

    move_ids = fields.Many2many('commission.move', string='Comisiones', required=True)
    note = fields.Char(string='Motivo / referencia del pago', required=True,
                       help='Ej. "Pagada en nómina de julio 2025", "Cheque 1234", "Liquidada antes del sistema".')
    total = fields.Monetary(compute='_compute_total', currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)

    @api.depends('move_ids.amount')
    def _compute_total(self):
        for w in self:
            w.total = sum(w.move_ids.mapped('amount'))

    def action_confirm(self):
        self.move_ids._mark_paid_outside(self.note)
        return {'type': 'ir.actions.act_window_close'}
