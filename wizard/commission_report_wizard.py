from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import date


class CommissionReportWizard(models.TransientModel):
    _name = 'commission.report.wizard'
    _description = 'Asistente de Reporte de Comisiones'

    date_from = fields.Date(string='Cobros desde', required=True)
    date_to = fields.Date(string='Cobros hasta', required=True)
    partner_ids = fields.Many2many('res.partner', string='Comisionistas',
                                   domain=lambda self: self.env['res.partner']._commission_beneficiary_domain(),
                                   help="Dejar vacío para imprimir todos")
    is_authorizer = fields.Boolean(compute='_compute_is_authorizer')
    start_date = fields.Date(compute='_compute_start_date', string='Inicio de comisiones')

    @api.depends_context('uid')
    def _compute_is_authorizer(self):
        is_auth = self.env.user.has_group('om_advanced_commission.group_commission_manager')
        for rec in self:
            rec.is_authorizer = is_auth

    def _compute_start_date(self):
        start = self.env['commission.move']._commission_start_date()
        for rec in self:
            rec.start_date = start

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        today = date.today()
        start = self.env['commission.move']._commission_start_date()
        res['date_from'] = max(today.replace(day=1), start)
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
            'partner_ids': self.partner_ids.ids,
        }
        return self.env.ref('om_advanced_commission.action_report_commission_pdf').report_action(self, data=data)
