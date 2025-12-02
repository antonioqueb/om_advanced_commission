from odoo import models, fields, api

class CommissionReportWizard(models.TransientModel):
    _name = 'commission.report.wizard'
    _description = 'Asistente de Reporte de Comisiones'

    date_from = fields.Date(string='Desde', required=True, default=fields.Date.context_today)
    date_to = fields.Date(string='Hasta', required=True, default=fields.Date.context_today)
    partner_ids = fields.Many2many('res.partner', string='Vendedores', help="Dejar vacío para imprimir todos")

    def action_print_report(self):
        data = {
            'date_from': self.date_from,
            'date_to': self.date_to,
            'partner_ids': self.partner_ids.ids,
        }
        # Llama a la acción de reporte definida en el XML
        return self.env.ref('om_advanced_commission.action_report_commission_pdf').report_action(self, data=data)