from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import date


class CommissionReportWizard(models.TransientModel):
    _name = 'commission.report.wizard'
    _description = 'Asistente de Reporte de Comisiones'

    date_from = fields.Date(string='Desde', required=True)
    date_to = fields.Date(string='Hasta', required=True)
    partner_ids = fields.Many2many('res.partner', string='Vendedores',
                                   help="Dejar vacío para imprimir todos")
    allow_previous_months = fields.Boolean(string='Ver meses anteriores', default=False)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        today = date.today()
        res['date_from'] = today.replace(day=1)
        res['date_to'] = today
        is_auth = self.env.user.has_group('om_advanced_commission.group_commission_authorizer')
        res['allow_previous_months'] = is_auth
        if not is_auth:
            partner = self.env.user.partner_id
            res['partner_ids'] = [(6, 0, [partner.id])]
        return res

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        is_auth = self.env.user.has_group('om_advanced_commission.group_commission_authorizer')
        for rec in self:
            today = date.today()
            current_month_start = today.replace(day=1)
            if not is_auth:
                if rec.date_from and rec.date_from < current_month_start:
                    raise UserError(
                        "Solo puedes consultar comisiones del mes en curso. "
                        "Para ver meses anteriores, solicita acceso a un autorizador."
                    )
                if rec.date_to and rec.date_to > today:
                    raise UserError("La fecha 'Hasta' no puede ser mayor a hoy.")

    def action_print_report(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_authorizer'):
            partner = self.env.user.partner_id
            if self.partner_ids and partner not in self.partner_ids:
                raise UserError("Solo puedes ver tus propias comisiones.")
            if not self.partner_ids:
                self.partner_ids = [(6, 0, [partner.id])]

        data = {
            'date_from': self.date_from,
            'date_to': self.date_to,
            'partner_ids': self.partner_ids.ids,
        }
        return self.env.ref('om_advanced_commission.action_report_commission_pdf').report_action(self, data=data)