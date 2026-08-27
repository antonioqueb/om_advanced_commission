from datetime import datetime, time as dtime

import pytz

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

        if not date_from or not date_to:
            return {
                'doc_ids': docids,
                'doc_model': 'commission.report.wizard',
                'data': data,
                'docs': [],
                'company': self.env.company,
            }

        # FECHA DE NEGOCIO: el periodo se corta por la FECHA DE LA ORDEN
        # (date_order), no por la fecha del movimiento de comisión — esa nace
        # al cobrarse y arrastraba ventas de meses pasados al mes en curso.
        # date_order es Datetime en UTC: los límites del día se convierten
        # desde la zona horaria del usuario para no perder/robar órdenes en
        # los bordes del mes. Movimientos sin orden (ajustes manuales) se
        # cortan por su propia fecha.
        date_from_d = fields.Date.to_date(date_from)
        date_to_d = fields.Date.to_date(date_to)
        tz = pytz.timezone(self.env.user.tz or 'America/Mexico_City')
        start_utc = tz.localize(
            datetime.combine(date_from_d, dtime.min)
        ).astimezone(pytz.utc).replace(tzinfo=None)
        end_utc = tz.localize(
            datetime.combine(date_to_d, dtime.max)
        ).astimezone(pytz.utc).replace(tzinfo=None)

        domain = [
            ('state', '!=', 'cancel'),
            ('company_id', '=', self.env.company.id),
            '|',
                '&', ('sale_order_id', '!=', False),
                     '&', ('sale_order_id.date_order', '>=', start_utc),
                          ('sale_order_id.date_order', '<=', end_utc),
                '&', ('sale_order_id', '=', False),
                     '&', ('date', '>=', date_from),
                          ('date', '<=', date_to),
        ]

        # CANDADO DE PROPIEDAD: quien no es autorizador SOLO puede imprimir
        # sus propias comisiones, sin importar qué traiga `data` (el wizard
        # ya lo fuerza, esto lo garantiza aunque el reporte se invoque por
        # otra vía). El mismo criterio que la record rule del modelo.
        if self.env.user.has_group(
                'om_advanced_commission.group_commission_authorizer'):
            if partner_ids:
                domain.append(('partner_id', 'in', partner_ids))
        else:
            domain.append(
                ('partner_id.user_ids', 'in', [self.env.user.id]))

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