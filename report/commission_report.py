from odoo import models, api, _

class ReportCommissionPDF(models.AbstractModel):
    _name = 'report.om_advanced_commission.report_commission_document'
    _description = 'Lógica de Reporte de Comisiones'

    @api.model
    def _get_report_values(self, docids, data=None):
        date_from = data.get('date_from')
        date_to = data.get('date_to')
        partner_ids = data.get('partner_ids')

        # Construir dominio
        domain = [
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('state', '!=', 'cancel') # Excluir cancelados
        ]
        
        if partner_ids:
            domain.append(('partner_id', 'in', partner_ids))

        # Buscar movimientos
        moves = self.env['commission.move'].search(domain, order='partner_id, date, id')

        # Agrupar por Partner
        grouped_data = {}
        for move in moves:
            partner = move.partner_id
            if partner not in grouped_data:
                grouped_data[partner] = {
                    'partner': partner,
                    'moves': [],
                    'total_base': 0.0,
                    'total_commission': 0.0
                }
            
            grouped_data[partner]['moves'].append(move)
            grouped_data[partner]['total_base'] += move.base_amount_paid
            grouped_data[partner]['total_commission'] += move.amount

        return {
            'doc_ids': docids,
            'doc_model': 'commission.report.wizard',
            'data': data,
            'docs': grouped_data.values(), # Pasamos la lista agrupada
            'company': self.env.company,
        }