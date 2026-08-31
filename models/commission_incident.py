from datetime import timedelta

from odoo import models, fields, api
from odoo.exceptions import UserError


class CommissionIncident(models.Model):
    """Incidencias de cobro que afectan comisiones: montos que no cuadran.

    Se detectan SOLAS (al conciliar, al publicar un pago y en una pasada
    diaria de recibos de caja). Un saldo a favor es válido; uno desmedido
    (o con pinta de dígito de más) se marca y bloquea la liquidación de
    las comisiones de ese pago hasta que un administrador lo resuelva."""
    _name = 'commission.incident'
    _description = 'Incidencia de cobro (comisiones)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(string='Referencia', readonly=True, default='/')
    kind = fields.Selection([
        ('digits', 'Posible dígito de más'),
        ('overpayment', 'Sobrepago desmedido'),
        ('receipt_mismatch', 'Recibo de caja ≠ pago'),
        ('over_order', 'Cobrado supera la venta'),
    ], string='Tipo', required=True, index=True)
    severity = fields.Selection([
        ('high', 'Alta'), ('medium', 'Media'), ('low', 'Baja')], string='Severidad', default='medium')
    state = fields.Selection([
        ('open', 'Abierta'), ('resolved', 'Resuelta'), ('ignored', 'Ignorada')],
        default='open', tracking=True, index=True)

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', string='Cliente', index=True)
    payment_id = fields.Many2one('account.payment', string='Pago')
    payment_move_id = fields.Many2one('account.move', string='Asiento del Pago', index=True)
    invoice_id = fields.Many2one('account.move', string='Factura')
    sale_order_id = fields.Many2one('sale.order', string='Orden', index=True)

    amount_expected = fields.Monetary(string='Esperado', help='Lo adeudado / aplicado / registrado en recibos.')
    amount_actual = fields.Monetary(string='Registrado', help='Lo que realmente se capturó en el pago.')
    amount_diff = fields.Monetary(string='Diferencia')
    ratio = fields.Float(string='Registrado / Esperado', digits=(12, 2))
    note = fields.Text(string='Detalle')
    resolution = fields.Text(string='Resolución')
    resolved_by = fields.Many2one('res.users', readonly=True)
    resolved_date = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = (self.env['res.company'].browse(vals['company_id'])
                           if vals.get('company_id') else self.env.company)
                vals['name'] = self.env['commission.move']._som_next_sequence(
                    'commission.incident', company) or 'INC'
        recs = super().create(vals_list)
        recs._notify()
        return recs

    # ------------------------------------------------------------------
    # Parámetros
    # ------------------------------------------------------------------
    @api.model
    def _tolerances(self):
        P = self.env['ir.config_parameter'].sudo()
        try:
            abs_tol = float(P.get_param('om_advanced_commission.incident_abs_tolerance', '1000') or 0)
        except (TypeError, ValueError):
            abs_tol = 1000.0
        try:
            pct_tol = float(P.get_param('om_advanced_commission.incident_pct_tolerance', '10') or 0)
        except (TypeError, ValueError):
            pct_tol = 10.0
        try:
            digits_ratio = float(P.get_param('om_advanced_commission.incident_digits_ratio', '9.5') or 9.5)
        except (TypeError, ValueError):
            digits_ratio = 9.5
        return max(abs_tol, 0.0), max(pct_tol, 0.0) / 100.0, max(digits_ratio, 2.0)

    @api.model
    def _excess_allowed(self, expected):
        abs_tol, pct_tol, _d = self._tolerances()
        return max(abs_tol, abs(expected) * pct_tol)

    # ------------------------------------------------------------------
    # Alta con dedupe + aviso
    # ------------------------------------------------------------------
    @api.model
    def _open(self, kind, vals):
        dom = [('kind', '=', kind), ('state', '=', 'open')]
        for key in ('payment_move_id', 'sale_order_id', 'payment_id'):
            if vals.get(key):
                dom.append((key, '=', vals[key]))
                break
        existing = self.sudo().search(dom, limit=1)
        if existing:
            existing.write({k: v for k, v in vals.items() if k in ('amount_expected', 'amount_actual',
                                                                    'amount_diff', 'ratio', 'note', 'severity')})
            return existing
        return self.sudo().create(dict(vals, kind=kind))

    def _notify(self):
        Move = self.env['commission.move']
        for inc in self:
            body = ('⚠️ Incidencia %s · %s: registrado %s vs esperado %s (dif. %s). %s'
                    % (inc.name, dict(inc._fields['kind'].selection)[inc.kind],
                       inc.amount_actual, inc.amount_expected, inc.amount_diff, inc.note or ''))
            for target in (inc.sale_order_id, inc.payment_id):
                if target:
                    target.sudo().message_post(body=body, message_type='notification')
            for user in Move._commission_manager_users():
                inc.activity_schedule('mail.mail_activity_data_todo', user_id=user.id,
                                      summary='Incidencia de cobro', note=body)

    # ------------------------------------------------------------------
    # Detectores
    # ------------------------------------------------------------------
    @api.model
    def _classify(self, expected, actual):
        """None si está dentro de tolerancia; si no, (kind, severity)."""
        expected = abs(expected or 0.0)
        actual = abs(actual or 0.0)
        excess = actual - expected
        if excess <= self._excess_allowed(expected):
            return None
        _a, _p, digits_ratio = self._tolerances()
        if expected and actual / expected >= digits_ratio:
            return ('digits', 'high')
        return ('overpayment', 'medium')

    @api.model
    def _detect_for_partials(self, partials):
        """Al conciliar: por cada asiento de pago involucrado compara lo
        aplicado contra lo capturado; el sobrante es saldo a favor."""
        by_payment = {}
        for p in partials.sudo():
            side = p._commission_invoice_side()
            if not side:
                continue
            invoice, counterpart, _il, _ol = side
            if counterpart.move_type != 'entry':
                continue
            by_payment.setdefault(counterpart, self.env['account.move']) 
            by_payment[counterpart] |= invoice
        for pay_move, invoices in by_payment.items():
            company = pay_move.company_id
            ccur = company.currency_id
            recv = pay_move.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
            if not recv:
                continue
            actual = sum(abs(l.balance) for l in recv)
            unapplied = sum(abs(l.amount_residual) for l in recv)
            applied = actual - unapplied
            if applied <= 0:
                continue
            verdict = self._classify(applied, actual)
            payment = self.env['account.payment'].sudo().search([('move_id', '=', pay_move.id)], limit=1)
            so = invoices.mapped('invoice_line_ids.sale_line_ids.order_id')[:1]
            if verdict:
                kind, sev = verdict
                self._open(kind, {
                    'severity': sev,
                    'company_id': company.id,
                    'partner_id': pay_move.partner_id.id or invoices[:1].partner_id.id,
                    'payment_id': payment.id,
                    'payment_move_id': pay_move.id,
                    'invoice_id': invoices[:1].id,
                    'sale_order_id': so.id,
                    'amount_expected': ccur.round(applied),
                    'amount_actual': ccur.round(actual),
                    'amount_diff': ccur.round(actual - applied),
                    'ratio': round(actual / applied, 2) if applied else 0.0,
                    'note': ('El pago %s deja %s de saldo a favor tras aplicarse a %s. '
                             'Verifica el monto capturado.' % (
                                 pay_move.name, ccur.round(unapplied), ', '.join(invoices.mapped('name')))),
                })
            # Consistencia: cobrado acumulado de la orden vs. su subtotal
            for order in invoices.mapped('invoice_line_ids.sale_line_ids.order_id'):
                self._check_over_order(order)

    @api.model
    def _check_over_order(self, order):
        order = order.sudo()
        if not order or not order.commission_rule_ids:
            return
        company = order.company_id
        ccur = company.currency_id
        order_untaxed = order.currency_id._convert(
            order.amount_untaxed, ccur, company, order.date_order or fields.Date.today())
        paid_base = order.commission_paid_base
        if order_untaxed and paid_base > order_untaxed + self._excess_allowed(order_untaxed):
            self._open('over_order', {
                'severity': 'low',
                'company_id': company.id,
                'partner_id': order.partner_id.id,
                'sale_order_id': order.id,
                'amount_expected': ccur.round(order_untaxed),
                'amount_actual': ccur.round(paid_base),
                'amount_diff': ccur.round(paid_base - order_untaxed),
                'ratio': round(paid_base / order_untaxed, 2),
                'note': 'La base cobrada comisionable de %s supera el subtotal de la orden.' % order.name,
            })

    @api.model
    def _detect_for_payment(self, payment):
        """Al publicar un pago de cliente: compara contra el adeudo total del
        cliente (antes de aplicarse). Atrapa el dígito de más aunque el pago
        no se aplique a factura alguna."""
        payment = payment.sudo()
        if payment.payment_type != 'inbound' or payment.partner_type != 'customer' or not payment.partner_id:
            return
        company = payment.company_id
        ccur = company.currency_id
        actual = payment.currency_id._convert(payment.amount, ccur, company, payment.date or fields.Date.today())
        partner = payment.partner_id.commercial_partner_id
        debt = partner.with_company(company).credit or 0.0  # cuentas por cobrar abiertas
        if debt <= 0:
            return
        verdict = self._classify(debt, actual)
        if not verdict:
            return
        kind, sev = verdict
        self._open(kind, {
            'severity': sev,
            'company_id': company.id,
            'partner_id': partner.id,
            'payment_id': payment.id,
            'payment_move_id': payment.move_id.id if payment.move_id else False,
            'amount_expected': ccur.round(debt),
            'amount_actual': ccur.round(actual),
            'amount_diff': ccur.round(actual - debt),
            'ratio': round(actual / debt, 2),
            'note': 'El pago %s (%s) supera el adeudo total del cliente (%s).' % (
                payment.name, ccur.round(actual), ccur.round(debt)),
        })

    @api.model
    def _detect_receipt_mismatches(self, days=60):
        """Recibos de caja ligados a un pago cuyo total no coincide con el
        monto del pago (cash_receipt_voucher, si está instalado)."""
        if 'cash.receipt' not in self.env:
            return
        Receipt = self.env['cash.receipt'].sudo()
        since = fields.Date.context_today(self) - timedelta(days=days)
        # sudo salta las reglas: el cron itera compañías con with_company.
        receipts = Receipt.search([('payment_id', '!=', False), ('create_date', '>=', since),
                                   ('company_id', '=', self.env.company.id)])
        by_payment = {}
        for r in receipts:
            by_payment.setdefault(r.payment_id, Receipt)
            by_payment[r.payment_id] |= r
        for payment, recs in by_payment.items():
            company = payment.company_id
            ccur = company.currency_id
            date = payment.date or fields.Date.today()
            expected = sum(r.currency_id._convert(r.amount, ccur, company, date) if r.currency_id else r.amount
                           for r in recs)
            actual = payment.currency_id._convert(payment.amount, ccur, company, date)
            if abs(actual - expected) <= self._excess_allowed(expected):
                continue
            self._open('receipt_mismatch', {
                'severity': 'high' if actual > expected else 'medium',
                'company_id': company.id,
                'partner_id': payment.partner_id.id,
                'payment_id': payment.id,
                'payment_move_id': payment.move_id.id if payment.move_id else False,
                'amount_expected': ccur.round(expected),
                'amount_actual': ccur.round(actual),
                'amount_diff': ccur.round(actual - expected),
                'ratio': round(actual / expected, 2) if expected else 0.0,
                'note': 'Recibos %s suman %s; el pago %s registra %s.' % (
                    ', '.join(recs.mapped('name')), ccur.round(expected), payment.name, ccur.round(actual)),
            })

    @api.model
    def _cron_detect(self):
        for company in self.env['res.company'].search([]):
            self.with_company(company)._detect_receipt_mismatches()

    # ------------------------------------------------------------------
    # Bloqueo de liquidación
    # ------------------------------------------------------------------
    @api.model
    def _blocked_commission_moves(self, moves):
        """Movimientos de comisión cuyo cobro tiene incidencia ABIERTA."""
        if not moves:
            return moves.browse()
        open_inc = self.sudo().search([('state', '=', 'open'), ('kind', '!=', 'over_order'),
                                       ('company_id', 'in', moves.mapped('company_id').ids)])
        if not open_inc:
            return moves.browse()
        pay_moves = open_inc.mapped('payment_move_id')
        payments = open_inc.mapped('payment_id')
        Partial = self.env['account.partial.reconcile'].sudo()
        partials = Partial.search([
            '|', ('debit_move_id.move_id', 'in', pay_moves.ids),
                 ('credit_move_id.move_id', 'in', pay_moves.ids)]) if pay_moves else Partial
        return moves.filtered(lambda m: (m.payment_id and m.payment_id in payments)
                              or (m.partial_reconcile_id and m.partial_reconcile_id in partials))

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def _check_manager(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_manager'):
            raise UserError("Solo un Administrador de Comisiones puede cerrar incidencias.")

    def _close(self, state):
        self._check_manager()
        for inc in self:
            if not inc.resolution:
                raise UserError("Escribe la resolución (qué se revisó o corrigió) antes de cerrar la incidencia.")
            inc.write({'state': state, 'resolved_by': self.env.user.id, 'resolved_date': fields.Datetime.now()})
            inc.activity_ids.unlink()
            inc.message_post(body='%s por %s: %s' % (
                'Resuelta' if state == 'resolved' else 'Ignorada (saldo a favor aceptado)',
                self.env.user.name, inc.resolution))

    def action_resolve(self):
        self._close('resolved')

    def action_ignore(self):
        self._close('ignored')

    def action_reopen(self):
        self._check_manager()
        self.write({'state': 'open'})
        self._notify()


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def action_post(self):
        res = super().action_post()
        Incident = self.env['commission.incident'].sudo()
        for payment in self:
            try:
                Incident._detect_for_payment(payment)
            except Exception:  # noqa: BLE001 — jamás bloquear un pago
                import logging
                logging.getLogger(__name__).exception("[COMMISSION] incidencia no evaluada en pago %s", payment.id)
        return res
