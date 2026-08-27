from calendar import monthrange
from datetime import date, datetime, time as dtime, timedelta

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
    invoice_id = fields.Many2one('account.move', string='Factura', index=True)
    # related = se lee con sudo: el vendedor ve el folio sin permisos contables
    invoice_name = fields.Char(related='invoice_id.name', string='Factura', store=True)
    invoice_line_id = fields.Many2one('account.move.line', string='Línea de Factura Origen')
    payment_id = fields.Many2one('account.payment', string='Pago Cliente')
    partial_reconcile_id = fields.Many2one('account.partial.reconcile', string='Conciliación Origen',
                                           index=True, ondelete='set null')

    settlement_id = fields.Many2one('commission.settlement', string='Liquidación', ondelete='set null')
    company_id = fields.Many2one('res.company', string='Compañía', required=True,
                                 default=lambda self: self.env.company, index=True)

    amount = fields.Monetary(string='Monto Comisión', currency_field='currency_id')
    base_amount_paid = fields.Monetary(string='Base Cobrada',
                                       help='Monto sin impuestos del pago que generó esta comisión')
    currency_id = fields.Many2one('res.currency', required=True)

    date = fields.Date(default=fields.Date.context_today, index=True)

    is_refund = fields.Boolean(string='Es Devolución', default=False)

    # ── Snapshot de la regla al momento del devengo (explica cada monto
    # aunque la regla cambie o desaparezca después) ────────────────────
    rule_id = fields.Many2one('sale.commission.rule', string='Regla', ondelete='set null', index=True)
    rule_role = fields.Selection([
        ('internal', 'Vendedor'), ('architect', 'Embajador'),
        ('construction', 'Constructora'), ('referrer', 'Referidor')], string='Rol', readonly=True)
    rule_base = fields.Selection([
        ('amount_untaxed', 'Monto Base (Subtotal)'), ('amount_total', 'Monto Total (Inc. Impuestos)'),
        ('margin', 'Margen (Ganancia)'), ('gross_utility', 'Utilidad Bruta'), ('manual', 'Manual / Fijo')],
        string='Base', readonly=True)
    rule_percent = fields.Float(string='% Regla', readonly=True)
    rule_fixed_amount = fields.Monetary(string='Fijo Regla', readonly=True)
    retention_factor = fields.Float(string='Factor Aplicado', default=1.0, readonly=True,
                                    help='1.0 = sin retención. Menor a 1 = excedente retenido por autorización pendiente.')

    # ── Reversas (nunca se borra lo ya liquidado: se revierte) ─────────
    origin_move_id = fields.Many2one('commission.move', string='Ajuste de', readonly=True, index=True)
    reversal_ids = fields.One2many('commission.move', 'origin_move_id', string='Ajustes / Reversas')
    adjustment_kind = fields.Selection([
        ('reversal', 'Reversa (cobro desconciliado)'),
        ('adjustment', 'Ajuste (regla o autorización cambió)'),
    ], string='Tipo de Ajuste', readonly=True)
    is_reversal = fields.Boolean(compute='_compute_is_reversal', store=True)

    state = fields.Selection([
        ('draft', 'Pendiente'),
        ('settled', 'En Liquidación'),
        ('invoiced', 'Pagado'),
        ('cancel', 'Cancelado')
    ], default='draft', string='Estado', index=True)

    # Odoo 19: models.Constraint reemplaza a _sql_constraints. Una comisión
    # por (conciliación, beneficiario, orden, regla); las reversas viajan
    # con partial NULL y no colisionan.
    _unique_commission_per_reconcile_partner_rule = models.Constraint(
        'UNIQUE(partial_reconcile_id, partner_id, sale_order_id, rule_id)',
        'Ya existe una comisión para esta conciliación, comisionista, orden y regla.',
    )

    @api.depends('origin_move_id', 'adjustment_kind')
    def _compute_is_reversal(self):
        for m in self:
            m.is_reversal = bool(m.origin_move_id) and m.adjustment_kind != 'adjustment'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('commission.move') or 'COMM'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Helpers de grupo (Odoo 19: user_ids solo trae miembros directos)
    # ------------------------------------------------------------------
    @api.model
    def _commission_group_users(self, xmlid):
        Users = self.env['res.users']
        group = self.env.ref(xmlid, raise_if_not_found=False)
        if not group:
            return Users
        users = Users
        for fname in ('all_user_ids', 'user_ids', 'users'):
            if fname in group._fields:
                users |= group[fname]
        if not users:
            for fname in ('all_group_ids', 'group_ids', 'groups_id'):
                if fname in Users._fields:
                    users = Users.search([(fname, 'in', group.id)])
                    break
        return users.filtered(lambda u: u.active and not u.share)

    @api.model
    def _commission_manager_users(self):
        return self._commission_group_users('om_advanced_commission.group_commission_manager')

    @api.model
    def _commission_authorizer_users(self):
        return self._commission_group_users('om_advanced_commission.group_commission_authorizer')

    # ------------------------------------------------------------------
    # Familia (original + ajustes/reversas) y neutralización
    # ------------------------------------------------------------------
    def _commission_family(self):
        """Original + sus ajustes y reversas vivos."""
        self.ensure_one()
        return (self | self.reversal_ids).filtered(lambda m: m.state != 'cancel')

    def _commission_is_paid(self):
        self.ensure_one()
        if self.state == 'invoiced':
            return True
        return self.state == 'settled' and bool(self.settlement_id) and self.settlement_id.state == 'invoiced'

    def _commission_neutralize(self, kind, reason):
        """Deja la familia de cada original en CERO neto, en tiempo real:
        - lo no pagado se borra (o se cancela el original si tiene hijos pagados);
        - lo ya pagado se compensa con un movimiento negativo pendiente."""
        Move = self.env['commission.move'].sudo()
        for move in self.filtered(lambda m: not m.origin_move_id and m.state != 'cancel'):
            so = move.sale_order_id
            family = move._commission_family()
            paid = family.filtered(lambda m: m._commission_is_paid())
            unpaid = family - paid
            for m in unpaid:
                if m.settlement_id:
                    m.settlement_id.message_post(
                        body='Movimiento %s retirado: %s.' % (m.name, reason))
                    m.settlement_id = False
            names = ', '.join(unpaid.mapped('name'))
            if not paid:
                unpaid.unlink()
                if so and names:
                    so.message_post(body='Comisión %s eliminada: %s.' % (names, reason),
                                    message_type='notification')
                continue
            # Hay dinero ya pagado: el original se conserva como ancla.
            (unpaid - move).unlink()
            if move in unpaid:
                move.write({'state': 'cancel', 'settlement_id': False})
            total_paid = sum(paid.mapped('amount'))
            if move.currency_id.is_zero(total_paid):
                continue
            rev = Move.create({
                'name': ('REV ' if kind == 'reversal' else 'AJU ') + move.name,
                'partner_id': move.partner_id.id,
                'sale_order_id': so.id if so else False,
                'invoice_id': move.invoice_id.id,
                'invoice_line_id': move.invoice_line_id.id,
                'payment_id': move.payment_id.id,
                'company_id': move.company_id.id,
                'currency_id': move.currency_id.id,
                'amount': -total_paid,
                'base_amount_paid': 0.0,
                'date': fields.Date.context_today(self),
                'state': 'draft',
                'origin_move_id': move.id,
                'adjustment_kind': kind,
                'rule_id': move.rule_id.id,
                'rule_role': move.rule_role,
                'rule_base': move.rule_base,
                'rule_percent': move.rule_percent,
                'rule_fixed_amount': move.rule_fixed_amount,
                'retention_factor': move.retention_factor,
                'is_refund': move.is_refund,
            })
            body = ('Comisión %s ya pagada; %s. Se creó %s por %s que se descontará en la '
                    'siguiente liquidación.' % (move.name, reason, rev.name, rev.amount))
            if so:
                so.message_post(body=body, message_type='notification')
                for user in self._commission_manager_users():
                    so.activity_schedule('mail.mail_activity_data_todo', user_id=user.id,
                                         summary='Reversa de comisión', note=body)

    def _on_partial_unreconciled(self):
        """Se llama ANTES de borrar el partial."""
        self._commission_neutralize('reversal', 'el cobro fue desconciliado')

    def _commission_apply_expected(self, expected, vals, reason):
        """Alinea la familia de este original al monto esperado (tiempo real).
        Original pendiente y sin hijos → se corrige en sitio. Si ya hubo pago
        → ajuste pendiente por el delta (nunca se toca lo pagado)."""
        self.ensure_one()
        cur = self.currency_id
        family = self._commission_family()
        current = sum(family.mapped('amount'))
        delta = cur.round(expected - current)
        if cur.is_zero(delta):
            return False
        children = family - self
        if self.state == 'draft' and not children:
            self.write(dict(vals, amount=cur.round(expected)))
            return True
        draft_adj = children.filtered(lambda m: m.state == 'draft' and m.adjustment_kind == 'adjustment')[:1]
        if draft_adj:
            draft_adj.write({'amount': cur.round(draft_adj.amount + delta),
                             'retention_factor': vals.get('retention_factor', draft_adj.retention_factor),
                             'date': fields.Date.context_today(self)})
            return True
        self.env['commission.move'].sudo().create({
            'name': 'AJU ' + self.name,
            'partner_id': self.partner_id.id,
            'sale_order_id': self.sale_order_id.id,
            'invoice_id': self.invoice_id.id,
            'invoice_line_id': self.invoice_line_id.id,
            'payment_id': self.payment_id.id,
            'company_id': self.company_id.id,
            'currency_id': cur.id,
            'amount': delta,
            'base_amount_paid': 0.0,
            'date': fields.Date.context_today(self),
            'state': 'draft',
            'origin_move_id': self.id,
            'adjustment_kind': 'adjustment',
            'rule_id': vals.get('rule_id', self.rule_id.id),
            'rule_role': vals.get('rule_role', self.rule_role),
            'rule_base': vals.get('rule_base', self.rule_base),
            'rule_percent': vals.get('rule_percent', self.rule_percent),
            'rule_fixed_amount': vals.get('rule_fixed_amount', self.rule_fixed_amount),
            'retention_factor': vals.get('retention_factor', self.retention_factor),
            'is_refund': self.is_refund,
        })
        if self.sale_order_id:
            self.sale_order_id.message_post(
                body='Ajuste de comisión %s por %s (%s).' % (self.name, delta, reason),
                message_type='notification')
        return True

    # ------------------------------------------------------------------
    # Auditoría: cobros sin comisión
    # ------------------------------------------------------------------
    @api.model
    def _commission_missing_partials(self, date_from=None, company=None):
        """Partials de cuentas por cobrar de facturas de cliente (posted)
        ligadas a órdenes con reglas, que no generaron comisión."""
        company = company or self.env.company
        date_from = date_from or (fields.Date.context_today(self) - timedelta(days=90))
        self.env.cr.execute("""
            SELECT DISTINCT apr.id
              FROM account_partial_reconcile apr
              JOIN account_move_line aml ON aml.id IN (apr.debit_move_id, apr.credit_move_id)
              JOIN account_move am ON am.id = aml.move_id
              JOIN account_account aa ON aa.id = aml.account_id
             WHERE am.move_type IN ('out_invoice', 'out_refund')
               AND am.state = 'posted'
               AND am.company_id = %s
               AND aa.account_type = 'asset_receivable'
               AND COALESCE(apr.max_date, apr.create_date::date) >= %s
               AND NOT EXISTS (SELECT 1 FROM commission_move cm
                                WHERE cm.partial_reconcile_id = apr.id)
        """, (company.id, date_from))
        ids = [r[0] for r in self.env.cr.fetchall()]
        partials = self.env['account.partial.reconcile'].sudo().browse(ids)
        result = self.env['account.partial.reconcile'].sudo()
        for p in partials:
            side = p._commission_invoice_side()
            if not side:
                continue
            if side[0]._commission_orders():
                result |= p
        return result

    @api.model
    def _cron_audit_missing_commissions(self):
        """Red de seguridad, no paso de proceso: el devengo es en tiempo real
        al conciliar; aquí solo se regenera lo que un error haya dejado fuera
        y se avisa únicamente de lo que siga faltando."""
        for company in self.env['res.company'].search([]):
            missing = self.with_company(company)._commission_missing_partials(company=company)
            if missing:
                missing._create_commission_moves()
                missing = self.with_company(company)._commission_missing_partials(company=company)
            if not missing:
                continue
            note = ('Hay %d cobro(s) de los últimos 90 días sin comisión generada. '
                    'Revísalos en Comisiones › Auditoría de Cobros.' % len(missing))
            Activity = self.env['mail.activity'].sudo()
            for user in self._commission_manager_users():
                partner = user.partner_id
                already = Activity.search_count([
                    ('user_id', '=', user.id), ('summary', '=', 'Cobros sin comisión'),
                    ('res_model', '=', 'res.partner'), ('res_id', '=', partner.id)])
                if already:
                    continue
                partner.sudo().activity_schedule(
                    'mail.mail_activity_data_todo', user_id=user.id,
                    summary='Cobros sin comisión', note=note)

    # ------------------------------------------------------------------
    # Dominio por periodo (un solo reloj, dos vistas explícitas)
    # ------------------------------------------------------------------
    @api.model
    def _commission_period_domain(self, date_from, date_to, basis='order'):
        """basis='order'  → corta por fecha de la VENTA (date_order).
           basis='payment' → corta por fecha del COBRO (move.date), que es
           lo que paga la liquidación y lo que suma el dashboard SOM."""
        if basis == 'payment':
            return [('date', '>=', date_from), ('date', '<=', date_to)]
        tz = pytz.timezone(self.env.user.tz or 'America/Mexico_City')
        start_utc = tz.localize(datetime.combine(date_from, dtime.min)).astimezone(pytz.utc).replace(tzinfo=None)
        end_utc = tz.localize(datetime.combine(date_to, dtime.max)).astimezone(pytz.utc).replace(tzinfo=None)
        return [
            '|',
                '&', ('sale_order_id', '!=', False),
                     '&', ('sale_order_id.date_order', '>=', start_utc),
                          ('sale_order_id.date_order', '<=', end_utc),
                '&', ('sale_order_id', '=', False),
                     '&', ('date', '>=', date_from),
                          ('date', '<=', date_to),
        ]

    # ------------------------------------------------------------------
    # Panel de Comisiones (client action OWL)
    # ------------------------------------------------------------------
    @api.model
    def get_commission_dashboard_data(self, month=None, partner_id=None, basis='order'):
        """Datos del Panel. Quien no es autorizador SOLO ve sus propias
        comisiones; el filtro por vendedor existe solo para autorizadores.
        Los datos contables se extraen con sudo (el vendedor no tiene
        permisos de contabilidad)."""
        user = self.env.user
        is_auth = user.has_group('om_advanced_commission.group_commission_authorizer')
        today = fields.Date.context_today(self)
        try:
            year, mon = [int(x) for x in (month or '').split('-')]
        except (ValueError, AttributeError):
            year, mon = today.year, today.month
        first = date(year, mon, 1)
        last = date(year, mon, monthrange(year, mon)[1])
        basis = 'payment' if basis == 'payment' else 'order'

        base_domain = [
            ('state', '!=', 'cancel'),
            ('company_id', '=', self.env.company.id),
        ] + self._commission_period_domain(first, last, basis)
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
            return cur._convert(amount or 0.0, company_cur, company, move.date or today)

        def fmt_dt(value):
            return format_date(self.env, value, date_format='dd MMM yyyy') if value else ''

        kpis = {'base': 0.0, 'total': 0.0, 'draft': 0.0,
                'settled': 0.0, 'invoiced': 0.0, 'retained': 0.0, 'count': len(moves)}
        rows = []
        for move in moves:
            sm = move.sudo()
            amt = to_company(move, move.amount)
            kpis['total'] += amt
            kpis['base'] += to_company(move, move.base_amount_paid)
            if move.state in kpis:
                kpis[move.state] += amt
            if move.retention_factor and 0 < move.retention_factor < 1.0:
                kpis['retained'] += amt / move.retention_factor - amt
            so = sm.sale_order_id
            inv = sm.invoice_id or sm.invoice_line_id.move_id
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
                'payment_date': fmt_dt(pay.date) if pay else fmt_dt(move.date),
                'base': round(to_company(move, move.base_amount_paid), 2),
                'amount': round(amt, 2),
                'state': move.state,
                'is_refund': move.is_refund or move.is_reversal,
                'kind': ('reversal' if move.adjustment_kind == 'reversal'
                         else 'adjustment' if move.adjustment_kind == 'adjustment'
                         else 'refund' if move.is_refund else ''),
                'retained': bool(move.retention_factor and 0 < move.retention_factor < 1.0),
            })
        for key in ('base', 'total', 'draft', 'settled', 'invoiced', 'retained'):
            kpis[key] = round(kpis[key], 2)

        sellers = []
        if is_auth:
            for partner in self.search(base_domain).mapped('partner_id').sorted('display_name'):
                sellers.append({'id': partner.id, 'name': partner.display_name})

        # Tendencia: 6 meses (el seleccionado incluido), mismo corte y filtros.
        trend = []
        short = ['ene', 'feb', 'mar', 'abr', 'may', 'jun',
                 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
        y, m = year, mon
        months = []
        for _ in range(6):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        for (ty, tm) in reversed(months):
            f = date(ty, tm, 1)
            l = date(ty, tm, monthrange(ty, tm)[1])
            dom = [('state', '!=', 'cancel'), ('company_id', '=', self.env.company.id)]
            dom += self._commission_period_domain(f, l, basis)
            if not is_auth:
                dom.append(('partner_id.user_ids', 'in', [user.id]))
            elif partner_id:
                dom.append(('partner_id', '=', int(partner_id)))
            total = 0.0
            for mv in self.search(dom):
                total += to_company(mv, mv.amount)
            trend.append({'key': '%04d-%02d' % (ty, tm), 'label': '%s %s' % (short[tm - 1], str(ty)[2:]),
                          'total': round(total, 2), 'current': (ty, tm) == (year, mon)})

        month_names = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
                       'julio', 'agosto', 'septiembre', 'octubre',
                       'noviembre', 'diciembre']
        return {
            'is_authorizer': is_auth,
            'basis': basis,
            'month': '%04d-%02d' % (year, mon),
            'month_label': '%s %s' % (month_names[mon - 1], year),
            'is_current_month': (year, mon) == (today.year, today.month),
            'currency_symbol': company_cur.symbol or '$',
            'kpis': kpis,
            'rows': rows,
            'sellers': sellers,
            'trend': trend,
            'updated_at': format_date(self.env, today, date_format='dd MMM yyyy'),
        }
