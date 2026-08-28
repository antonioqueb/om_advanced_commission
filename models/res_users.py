from odoo import models, fields


class ResUsers(models.Model):
    _inherit = 'res.users'

    commission_excluded = fields.Boolean(
        related='partner_id.commission_excluded', readonly=False,
        string='Excluido de comisiones')
