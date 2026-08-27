from calendar import monthrange
from datetime import date, datetime, time as dtime

import pytz

from odoo import models, fields, api
from odoo.tools.misc import format_date


class CommissionMove(models.Model):
    _name = 'commission.move'
    _description = 'Movimiento Individual de Comisión'
    _order = 'date desc, id desc'

    name = fields.Char(string='Referencia', required=True, default='/')

    partner_id = fields.Many2one('res.partner', string='Comisionista', required=True, index=True)
    sale_order_id = fields.Many2one('sale.order', string='Origen Venta', index=True)
    invoice_line_id = fields.Many2one('account.move.line', string='Línea de Factura Origen')
    payment_id = fields.Many2one('account.payment', string='Pago Cliente')
    partial_reconcile_id = fields.Many2one('account.partial.reconcile', string='Conciliación Origen', index=True)

    settlement_id = fields.Many2one('commission.settlement', string='Liquidación', ondelete='set null')
    company_id = fields.Many2one('res.company', string='Compañía', required=True,
                                 default=lambda self: self.env.company, index=True)

    amount = fields.Monetary(string='Monto Comisión', currency_field='currency_id')
    base_amount_paid = fields.Monetary(string='Base Cobrada',
                                       help='Monto sin impuestos del pago que generó esta comisión')
    currency_id = fields.Many2one('res.currency', required=True)

    date = fields.Date(default=fields.Date.context_today)

    is_refund = fields.Boolean(string='Es Devolución', default=False)
    state = fields.Selection([
        ('draft', 'Pendiente'),
        ('settled', 'En Liquidación'),
        ('invoiced', 'Facturado/Pagado'),
        ('cancel', 'Cancelado')
    ], default='draft', string='Estado', index=True)

    # Odoo 19: models.Constraint reemplaza a _sql_constraints (que ya no se
    # aplica). CRÍTICO aquí: sin esta restricción activa se podían duplicar
    # comisiones de la misma conciliación.
    _unique_commission_per_reconcile_partner_rule = models.Constraint(
        'UNIQUE(partial_reconcile_id, partner_id, sale_order_id)',
        'Ya existe una comisión para esta conciliación, comisionista y orden de venta.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('commission.move') or 'COMM'
        return super().create(vals_list)

    # ──────────────────────────────────────────────
    # Panel de Comisiones (client action OWL)
    # ──────────────────────────────────────────────

    @api.model
    def get_commission_dashboard_data(self, month=None, partner_id=None):
        """Datos del Panel de Comisiones.

        Mismas reglas que el reporte PDF: el periodo corta por la FECHA DE LA
        ORDEN (movimientos sin orden, por su propia fecha) y quien no es
        autorizador SOLO ve sus propias comisiones — el filtro por vendedor
        únicamente existe para autorizadores. Los datos contables (folio de
        factura, fecha de pago) se extraen con sudo porque el vendedor no
        tiene permisos de contabilidad.
        """
        user = self.env.user
        is_auth = user.has_group(
            'om_advanced_commission.group_commission_authorizer')
        today = fields.Date.context_today(self)
        try:
            year, mon = [int(x) for x in (month or '').split('-')]
        except (ValueError, AttributeError):
            year, mon = today.year, today.month
        first = date(year, mon, 1)
        last = date(year, mon, monthrange(year, mon)[1])
        tz = pytz.timezone(user.tz or 'America/Mexico_City')
        start_utc = tz.localize(
            datetime.combine(first, dtime.min)
        ).astimezone(pytz.utc).replace(tzinfo=None)
        end_utc = tz.localize(
            datetime.combine(last, dtime.max)
        ).astimezone(pytz.utc).replace(tzinfo=None)

        base_domain = [
            ('state', '!=', 'cancel'),
            ('company_id', '=', self.env.company.id),
            '|',
                '&', ('sale_order_id', '!=', False),
                     '&', ('sale_order_id.date_order', '>=', start_utc),
                          ('sale_order_id.date_order', '<=', end_utc),
                '&', ('sale_order_id', '=', False),
                     '&', ('date', '>=', first),
                          ('date', '<=', last),
        ]
        domain = list(base_domain)
        if not is_auth:
            domain.append(('partner_id.user_ids', 'in', [user.id]))
        elif partner_id:
            domain.append(('partner_id', '=', int(partner_id)))

        moves = self.search(domain, order='date desc, id desc')

        company = self.env.company
        company_cur = company.currency_id

        def to_company(move, amount):
            cur = move.currency_id
            if not cur or cur == company_cur:
                return amount or 0.0
            return cur._convert(
                amount or 0.0, company_cur, company, move.date or today)

        def fmt_dt(value):
            return format_date(
                self.env, value, date_format='dd MMM yyyy') if value else ''

        kpis = {'base': 0.0, 'total': 0.0, 'draft': 0.0,
                'settled': 0.0, 'invoiced': 0.0, 'count': len(moves)}
        rows = []
        for move in moves:
            sm = move.sudo()
            amt = to_company(move, move.amount)
            kpis['total'] += amt
            kpis['base'] += to_company(move, move.base_amount_paid)
            if move.state in kpis:
                kpis[move.state] += amt
            so = sm.sale_order_id
            inv = sm.invoice_line_id.move_id
            pay = sm.payment_id
            rows.append({
                'id': move.id,
                'name': move.name,
                'partner': move.partner_id.display_name,
                'order_id': so.id if so else False,
                'order': so.name if so else '',
                'order_date': fmt_dt(so.date_order) if so else fmt_dt(move.date),
                'customer': so.partner_id.display_name if so and so.partner_id else '',
                'invoice': (inv.name or '') if inv else '',
                'payment_date': fmt_dt(pay.date) if pay else '',
                'base': round(to_company(move, move.base_amount_paid), 2),
                'amount': round(amt, 2),
                'state': move.state,
                'is_refund': move.is_refund,
            })
        for key in ('base', 'total', 'draft', 'settled', 'invoiced'):
            kpis[key] = round(kpis[key], 2)

        sellers = []
        if is_auth:
            for partner in self.search(base_domain).mapped('partner_id').sorted('display_name'):
                sellers.append({'id': partner.id, 'name': partner.display_name})

        month_names = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
                       'julio', 'agosto', 'septiembre', 'octubre',
                       'noviembre', 'diciembre']
        return {
            'is_authorizer': is_auth,
            'month': '%04d-%02d' % (year, mon),
            'month_label': '%s %s' % (month_names[mon - 1], year),
            'is_current_month': (year, mon) == (today.year, today.month),
            'currency_symbol': company_cur.symbol or '$',
            'kpis': kpis,
            'rows': rows,
            'sellers': sellers,
        }