from odoo import fields, models, api
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
        basis = data.get('date_basis') or 'order'

        if not date_from or not date_to:
            return {
                'doc_ids': docids,
                'doc_model': 'commission.report.wizard',
                'data': data,
                'docs': [],
                'company': self.env.company,
                'basis_label': '',
            }

        Move = self.env['commission.move']
        date_from_d = fields.Date.to_date(date_from)
        date_to_d = fields.Date.to_date(date_to)

        # Un solo reloj, dos vistas explícitas: por fecha de VENTA (date_order)
        # o por fecha de COBRO (lo que paga la liquidación).
        domain = [
            ('state', '!=', 'cancel'),
            ('company_id', '=', self.env.company.id),
        ] + Move._commission_period_domain(date_from_d, date_to_d, basis)

        # CANDADO DE PROPIEDAD: quien no es autorizador SOLO imprime sus
        # propias comisiones, sin importar qué traiga `data`.
        if self.env.user.has_group('om_advanced_commission.group_commission_manager'):
            if partner_ids:
                domain.append(('partner_id', 'in', partner_ids))
        else:
            domain.append(('partner_id.user_ids', 'in', [self.env.user.id]))

        moves = Move.search(domain, order='partner_id, date, id')

        # Datos contables con sudo: el vendedor no tiene permisos de
        # contabilidad; solo se extraen folio y fechas.
        acct = {}
        for move in moves:
            sm = move.sudo()
            inv = sm.invoice_id or sm.invoice_line_id.move_id
            pay = sm.payment_id
            acct[move.id] = {
                'invoice_date': format_date(self.env, inv.invoice_date, date_format='dd MMM yyyy')
                if inv and inv.invoice_date else '',
                'invoice_name': (inv.name or '') if inv else '',
                'payment_date': format_date(self.env, pay.date, date_format='dd MMM yyyy')
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
                    'total_retained': 0.0,
                }
            g = grouped_data[partner.id]
            g['moves'].append(move)
            g['total_base'] += move.base_amount_paid
            g['total_commission'] += move.amount
            if move.retention_factor and 0 < move.retention_factor < 1.0:
                g['total_retained'] += move.amount / move.retention_factor - move.amount

        basis_label = ('por fecha de cobro' if basis == 'payment' else 'por fecha de venta')
        return {
            'doc_ids': docids,
            'doc_model': 'commission.report.wizard',
            'data': data,
            'docs': grouped_data.values(),
            'acct': acct,
            'company': self.env.company,
            'basis_label': basis_label,
        }
