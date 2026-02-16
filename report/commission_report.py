from odoo import models, api


class ReportCommissionPDF(models.AbstractModel):
    _name = 'report.om_advanced_commission.report_commission_document'
    _description = 'Lógica de Reporte de Comisiones'

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        date_from = data.get('date_from')
        date_to = data.get('date_to')
        partner_ids = data.get('partner_ids')

        if not date_from or not date_to:
            return {
                'doc_ids': docids,
                'doc_model': 'commission.report.wizard',
                'data': data,
                'docs': [],
                'company': self.env.company,
            }

        domain = [
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('state', '!=', 'cancel'),
            ('company_id', '=', self.env.company.id),
        ]
        if partner_ids:
            domain.append(('partner_id', 'in', partner_ids))

        moves = self.env['commission.move'].search(domain, order='partner_id, date, id')

        grouped_data = {}
        for move in moves:
            partner = move.partner_id
            if partner.id not in grouped_data:
                grouped_data[partner.id] = {
                    'partner': partner,
                    'currency': move.currency_id,
                    'moves': [],
                    'total_base': 0.0,
                    'total_commission': 0.0,
                }
            grouped_data[partner.id]['moves'].append(move)
            grouped_data[partner.id]['total_base'] += move.base_amount_paid
            grouped_data[partner.id]['total_commission'] += move.amount

        return {
            'doc_ids': docids,
            'doc_model': 'commission.report.wizard',
            'data': data,
            'docs': grouped_data.values(),
            'company': self.env.company,
        }