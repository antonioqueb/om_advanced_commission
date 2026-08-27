from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import date


class CommissionReportWizard(models.TransientModel):
    _name = 'commission.report.wizard'
    _description = 'Asistente de Reporte de Comisiones'

    date_from = fields.Date(string='Desde', required=True)
    date_to = fields.Date(string='Hasta', required=True)
    date_basis = fields.Selection([
        ('order', 'Por fecha de venta (devengado por orden)'),
        ('payment', 'Por fecha de cobro (lo que paga la liquidación)'),
    ], string='Periodo según', required=True, default='order')
    partner_ids = fields.Many2many('res.partner', string='Vendedores',
                                   help="Dejar vacío para imprimir todos")
    is_authorizer = fields.Boolean(compute='_compute_is_authorizer')

    @api.depends_context('uid')
    def _compute_is_authorizer(self):
        is_auth = self.env.user.has_group('om_advanced_commission.group_commission_manager')
        for rec in self:
            rec.is_authorizer = is_auth

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        today = date.today()
        res['date_from'] = today.replace(day=1)
        res['date_to'] = today
        if not self.env.user.has_group('om_advanced_commission.group_commission_manager'):
            res['partner_ids'] = [(6, 0, [self.env.user.partner_id.id])]
        return res

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_from and rec.date_to and rec.date_from > rec.date_to:
                raise UserError("La fecha 'Desde' no puede ser mayor a 'Hasta'.")

    def action_print_report(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_manager'):
            partner = self.env.user.partner_id
            # Cero excepciones: cualquier partner ajeno se rechaza y la
            # selección se fuerza al propio.
            foreign = self.partner_ids.filtered(lambda p: p != partner)
            if foreign:
                raise UserError("Solo puedes ver tus propias comisiones.")
            self.partner_ids = [(6, 0, [partner.id])]

        data = {
            'date_from': self.date_from,
            'date_to': self.date_to,
            'date_basis': self.date_basis,
            'partner_ids': self.partner_ids.ids,
        }
        return self.env.ref('om_advanced_commission.action_report_commission_pdf').report_action(self, data=data)
