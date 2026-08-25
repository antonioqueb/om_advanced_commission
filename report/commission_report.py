from odoo import models, api
from odoo.tools.misc import format_date


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

        # DATOS CONTABLES CON SUDO: el vendedor no tiene (ni debe tener)
        # permisos de contabilidad, y la plantilla leia factura/pago como
        # usuario -> AccessError de contabilidad al imprimir 'Mis
        # Comisiones'. Aqui se extraen SOLO folio y fechas, nada mas del
        # universo contable, y la plantilla los recibe ya resueltos.
        acct = {}
        for move in moves:
            sm = move.sudo()
            inv = sm.invoice_line_id.move_id
            pay = sm.payment_id
            acct[move.id] = {
                'invoice_date': format_date(
                    self.env, inv.invoice_date, date_format='dd MMM yyyy')
                if inv and inv.invoice_date else '',
                'invoice_name': (inv.name or '') if inv else '',
                'payment_date': format_date(
                    self.env, pay.date, date_format='dd MMM yyyy')
                if pay and pay.date else '',
            }

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
            'acct': acct,
            'company': self.env.company,
        }