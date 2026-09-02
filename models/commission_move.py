from calendar import monthrange
from datetime import date, datetime, time as dtime, timedelta

import pytz

from odoo import models, fields, api, _
from odoo.exceptions import UserError
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
    sale_order_ref = fields.Char(
        related='sale_order_id.client_order_ref',
        string='Referencia de la orden', store=True)
    project_id = fields.Many2one(
        related='sale_order_id.x_project_id',
        string='Proyecto (Job Name)', store=True)
    customer_id = fields.Many2one(
        related='sale_order_id.partner_id',
        string='Cliente', store=True)
    payment_date = fields.Date(
        string='Fecha de cobro', compute='_compute_payment_date', store=True,
        index=True,
        help='Fecha en que entró el dinero del cliente (pago o línea bancaria). '
             'Es el ÚNICO reloj de comisiones: panel, reporte y liquidación '
             'cortan por esta fecha. En reversas y ajustes es la fecha del '
             'ajuste (se descuenta en el corte en que ocurre).')

    @api.depends('payment_id.date', 'date', 'origin_move_id', 'partial_reconcile_id')
    def _compute_payment_date(self):
        for move in self:
            if move.origin_move_id:
                # Reversa/ajuste: cuenta en el periodo en que se genera.
                move.payment_date = move.date
                continue
            if move.payment_id.date:
                move.payment_date = move.payment_id.date
                continue
            cash_date = False
            partial = move.sudo().partial_reconcile_id
            if partial:
                try:
                    side = partial._commission_invoice_side()
                    cash_date = side[3].date if side else False
                except Exception:  # noqa: BLE001 - jamás tumbar el cómputo
                    cash_date = False
            move.payment_date = cash_date or move.date
    invoice_line_id = fields.Many2one('account.move.line', string='Línea de Factura Origen')
    payment_id = fields.Many2one('account.payment', string='Pago Cliente')
    partial_reconcile_id = fields.Many2one('account.partial.reconcile', string='Conciliación Origen',
                                           index=True, ondelete='set null')

    settlement_id = fields.Many2one('commission.settlement', string='Liquidación', ondelete='set null')
    company_id = fields.Many2one('res.company', string='Compañía', required=True,
                                 default=lambda self: self.env.company, index=True)

    amount = fields.Monetary(string='Monto Comisión', currency_field='currency_id')
    base_amount_paid = fields.Monetary(string='Cobrado sin IVA',
                                       help='Parte del cobro aplicada a esta orden, sin impuestos '
                                            '(excluye líneas marcadas "sin comisión").')
    # ── Transparencia del cálculo: cada movimiento se explica solo ──────
    amount_paid_total = fields.Monetary(
        string='Cobrado con IVA',
        help='Parte del cobro del cliente aplicada a esta orden, con impuestos. '
             'Es lo que el vendedor ve "entrar"; la comisión se calcula sin IVA.')
    commission_base = fields.Monetary(
        string='Base de cálculo',
        help='Importe sobre el que se aplicó el porcentaje: cobrado sin IVA, '
             'sin servicios si el rol no comisiona sobre ellos, y menos las '
             'comisiones externas en el caso de los vendedores.')
    external_deducted = fields.Monetary(
        string='Comisiones externas descontadas',
        help='Comisiones de embajadores/constructoras/referidores restadas de la '
             'base del vendedor en este cobro.')
    includes_services = fields.Boolean(
        string='Incluye servicios',
        help='Verdadero si la base incluyó fletes y otros servicios (según el rol '
             'configurado en Ajustes al momento del devengo).')
    pre_start = fields.Boolean(
        string='Anterior al inicio de comisiones', readonly=True, index=True,
        help='Cobro anterior a la fecha de inicio de comisiones (Ajustes). Se '
             'considera pagado fuera del sistema y no aparece en panel ni reportes.')
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

    # ── Cobrada fuera del sistema: el administrador identifica que esta
    # comisión ya se le pagó al vendedor (p. ej. antes del módulo) y la
    # marca para que NO vuelva a liquidarse. Queda como Pagado con rastro. ──
    external_paid = fields.Boolean(string='Cobrada fuera del sistema', readonly=True, index=True)
    external_paid_note = fields.Char(string='Motivo / referencia', readonly=True)
    external_paid_by = fields.Many2one('res.users', string='Marcó', readonly=True)
    external_paid_date = fields.Datetime(string='Fecha de marca', readonly=True)

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

    @api.model
    def _som_next_sequence(self, code, company=None):
        """next_by_code con la compañía del documento; si la compañía no tiene
        secuencia propia y la plantilla es de otra compañía, se clona para ella."""
        company = company or self.env.company
        Seq = self.env['ir.sequence'].sudo()
        name = Seq.with_company(company).next_by_code(code)
        if name:
            return name
        template = Seq.search([('code', '=', code)], order='company_id', limit=1)
        if not template:
            return False
        template.copy({
            'company_id': company.id,
            'number_next': 1,
            'name': '%s (%s)' % (template.name, company.name),
        })
        return Seq.with_company(company).next_by_code(code)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = (self.env['res.company'].browse(vals['company_id'])
                           if vals.get('company_id') else self.env.company)
                vals['name'] = self._som_next_sequence('commission.move', company) or 'COMM'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Marcar como cobrada (solo Administrador de Comisiones)
    # ------------------------------------------------------------------
    def _check_commission_manager(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_manager'):
            raise UserError(_('Solo un Administrador de Comisiones puede marcar comisiones como cobradas.'))

    def action_mark_paid_outside(self):
        """Abre el wizard (motivo obligatorio) para las comisiones seleccionadas."""
        self._check_commission_manager()
        movable = self.filtered(lambda m: m.state in ('draft', 'settled'))
        if not movable:
            raise UserError(_('Selecciona comisiones Pendientes o En liquidación (no pagadas).'))
        return {
            'type': 'ir.actions.act_window', 'res_model': 'commission.mark.paid.wizard',
            'view_mode': 'form', 'target': 'new', 'name': _('Marcar como cobrada'),
            'context': {'default_move_ids': [(6, 0, movable.ids)]},
        }

    def _mark_paid_outside(self, note):
        self._check_commission_manager()
        note = (note or '').strip()
        if not note:
            raise UserError(_('Escribe el motivo o la referencia del pago anterior.'))
        for move in self:
            if move.state == 'invoiced':
                continue
            if move.state == 'settled' and move.settlement_id and move.settlement_id.state == 'invoiced':
                raise UserError(_('%s ya está en una liquidación pagada.') % move.name)
            if move.settlement_id:
                move.settlement_id.message_post(
                    body=_('Movimiento %s retirado: marcado como cobrado fuera del sistema (%s).') % (move.name, note))
            move.write({
                'state': 'invoiced', 'settlement_id': False,
                'external_paid': True, 'external_paid_note': note,
                'external_paid_by': self.env.user.id, 'external_paid_date': fields.Datetime.now(),
            })
            if move.sale_order_id:
                move.sale_order_id.message_post(
                    body=_('Comisión %s de %s marcada como COBRADA fuera del sistema por %s: %s. No se liquidará.')
                    % (move.name, move.partner_id.display_name, self.env.user.name, note),
                    message_type='notification')
        return True

    def action_undo_paid_outside(self):
        """Revierte una marca hecha por error: vuelve a Pendiente."""
        self._check_commission_manager()
        for move in self.filtered('external_paid'):
            move.write({'state': 'draft', 'external_paid': False, 'pre_start': False})
            if move.sale_order_id:
                move.sale_order_id.message_post(
                    body=_('Se retiró la marca "cobrada fuera del sistema" de %s (por %s): vuelve a Pendiente.')
                    % (move.name, self.env.user.name), message_type='notification')
        return True

    # ------------------------------------------------------------------
    # Inicio de comisiones (corte NO retroactivo, siempre por fecha de cobro)
    # ------------------------------------------------------------------
    START_DATE_PARAM = 'om_advanced_commission.start_date'
    START_DATE_DEFAULT = date(2026, 8, 1)

    @api.model
    def _commission_start_date(self):
        """Primer día con comisiones vivas. Lo cobrado antes ya se pagó fuera
        del sistema: nace/queda como Pagado y no figura en panel ni reportes."""
        raw = self.env['ir.config_parameter'].sudo().get_param(self.START_DATE_PARAM)
        try:
            return fields.Date.to_date(raw) or self.START_DATE_DEFAULT
        except (ValueError, TypeError):
            return self.START_DATE_DEFAULT

    def _commission_close_before_start(self):
        """Cierra (Pagado · anterior al inicio) los movimientos de self cuyo
        cobro es previo al inicio de comisiones y aún no se han pagado. Sin
        asientos, sin liquidación: solo dejan de estar pendientes."""
        start = self._commission_start_date()
        closed = self.env['commission.move']
        for move in self:
            if move.state not in ('draft', 'settled') or not move.payment_date:
                continue
            if move.payment_date >= start:
                continue
            if move.settlement_id and move.settlement_id.state == 'invoiced':
                continue
            if move.settlement_id:
                move.settlement_id.message_post(
                    body=_('Movimiento %s retirado: cobro anterior al inicio de comisiones (%s).')
                    % (move.name, format_date(self.env, start, date_format='dd MMM yyyy')))
            move.write({
                'state': 'invoiced', 'settlement_id': False,
                'external_paid': True, 'pre_start': True,
                'external_paid_note': _('Cobro anterior al inicio de comisiones (%s): pagada fuera del sistema')
                % format_date(self.env, start, date_format='dd MMM yyyy'),
                'external_paid_by': self.env.user.id, 'external_paid_date': fields.Datetime.now(),
            })
            closed |= move
        return closed

    @api.model
    def _commission_apply_start_date(self):
        """Idempotente: cierra TODO lo pendiente con cobro anterior al inicio.
        Lo usa la migración y el botón de Ajustes (si la fecha se mueve)."""
        start = self._commission_start_date()
        pending = self.sudo().search([
            ('state', 'in', ('draft', 'settled')),
            ('payment_date', '<', start),
        ])
        return pending._commission_close_before_start()

    def _commission_update_info(self, info):
        """Actualiza los campos informativos del cálculo (cobrado con IVA,
        base, externos descontados) sin tocar el monto ni el estado."""
        for move in self:
            vals = {}
            cur = move.currency_id
            for key in ('amount_paid_total', 'commission_base', 'external_deducted'):
                if key in info and not cur.is_zero((move[key] or 0.0) - (info[key] or 0.0)):
                    vals[key] = info[key]
            if 'includes_services' in info and bool(move.includes_services) != bool(info['includes_services']):
                vals['includes_services'] = info['includes_services']
            if vals:
                move.write(vals)
        return True

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
        ligadas a una orden de venta (con o sin reglas) que no generaron
        comisión. Una orden SIN vendedor ni reglas también debe salir aquí:
        es dinero que entró y nadie comisiona."""
        company = company or self.env.company
        date_from = date_from or (fields.Date.context_today(self) - timedelta(days=90))
        # Lo anterior al inicio de comisiones no se audita.
        date_from = max(date_from, self._commission_start_date())
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
            if side[0]._commission_orders_any():
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
    def _commission_period_domain(self, date_from, date_to, basis='payment'):
        """Un solo reloj: la FECHA DE COBRO (payment_date). Las comisiones se
        pagan sobre lo cobrado, no sobre lo vendido; por eso el panel y el
        reporte ya no cortan por fecha de la orden. `basis='order'` se
        conserva únicamente por compatibilidad de llamadas externas."""
        if basis != 'order':
            return [('payment_date', '>=', date_from), ('payment_date', '<=', date_to)]
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
    # Cobros que NO generan comisión (para que el administrador los vea)
    # ------------------------------------------------------------------
    @api.model
    def _commission_unapplied_payments(self, date_from, date_to, limit=8):
        """Pagos de cliente del periodo con dinero SIN aplicar a factura.
        Ese dinero entró, pero no comisiona hasta que se aplique a una
        factura ligada a una orden (es lo que responde "recibí 180 mil y
        solo veo 125"). Montos en moneda de la compañía."""
        try:
            # savepoint: si el SQL falla, la transacción del panel sigue viva.
            with self.env.cr.savepoint():
                self.env.cr.execute("""
                    SELECT ap.id, ap.date, ap.amount, rc.symbol, rp.name,
                           ABS(l.amount_residual), ap.name
                      FROM account_payment ap
                      JOIN account_move pm ON pm.id = ap.move_id
                      JOIN account_move_line l ON l.move_id = pm.id
                      JOIN account_account a ON a.id = l.account_id
                 LEFT JOIN res_currency rc ON rc.id = ap.currency_id
                 LEFT JOIN res_partner rp ON rp.id = ap.partner_id
                     WHERE a.account_type = 'asset_receivable'
                       AND ap.partner_type = 'customer'
                       AND ap.payment_type = 'inbound'
                       AND pm.state = 'posted'
                       AND ap.date >= %s AND ap.date <= %s
                       AND pm.company_id IN %s
                       AND ABS(l.amount_residual) > 0.01
                     ORDER BY 6 DESC
                """, (date_from, date_to, tuple(self.env.companies.ids) or (0,)))
                rows = self.env.cr.fetchall()
        except Exception:  # noqa: BLE001 - el panel jamás se cae por esto
            return {'count': 0, 'total': 0.0, 'rows': []}
        total = sum(r[5] or 0.0 for r in rows)
        return {
            'count': len(rows),
            'total': round(total, 2),
            'rows': [{
                'id': r[0],
                'date': format_date(self.env, r[1], date_format='dd MMM yyyy') if r[1] else '',
                'amount': round(r[2] or 0.0, 2),
                'symbol': r[3] or '$',
                'customer': r[4] or '',
                'unapplied': round(r[5] or 0.0, 2),
                'name': r[6] or '',
            } for r in rows[:limit]],
        }

    @api.model
    def _commission_uncommissioned_partials(self, date_from, date_to, limit=8):
        """Cobros aplicados a facturas de órdenes que NO generaron comisión
        (orden sin vendedor ni reglas, o error al conciliar)."""
        try:
            partials = self._commission_missing_partials(date_from=date_from)
        except Exception:  # noqa: BLE001
            return {'count': 0, 'total': 0.0, 'rows': []}
        rows, total = [], 0.0
        for p in partials:
            cash = p.max_date
            if cash and cash > date_to:
                continue
            side = p._commission_invoice_side()
            if not side:
                continue
            invoice = side[0]
            orders = invoice._commission_orders_any()
            so = orders[:1]
            total += p.amount or 0.0
            rows.append({
                'id': p.id,
                'invoice': invoice.name or '',
                'order': so.name if so else '',
                'order_id': so.id if so else False,
                'customer': invoice.partner_id.display_name or '',
                'amount': round(p.amount or 0.0, 2),
                'date': format_date(self.env, cash, date_format='dd MMM yyyy') if cash else '',
                'reason': ('Orden sin vendedor ni reglas' if so and not so.commission_rule_ids
                           else 'Sin orden de venta' if not so else 'Sin movimiento (revisar)'),
            })
        rows.sort(key=lambda r: -r['amount'])
        return {'count': len(rows), 'total': round(total, 2), 'rows': rows[:limit]}

    # ------------------------------------------------------------------
    # Panel de Comisiones (client action OWL)
    # ------------------------------------------------------------------
    ROLE_LABELS = {
        'architect': 'Embajadores',
        'construction': 'Constructoras',
        'referrer': 'Referidores',
    }
    ROLE_SINGULAR = {
        'internal': 'Vendedor',
        'architect': 'Embajador',
        'construction': 'Constructora',
        'referrer': 'Referidor',
    }

    @api.model
    def get_commission_dashboard_data(self, month=None, partner_id=None, basis=None, scope='sellers'):
        """Datos del Panel de Comisiones.

        UN SOLO RELOJ: la fecha de cobro (`payment_date`). El parámetro
        `basis` se acepta por compatibilidad y se ignora. Nada anterior al
        inicio de comisiones (Ajustes) se muestra: ya se pagó fuera.

        Estructura: sección principal = VENDEDORES internos que comisionan
        en el mes, sección aparte = comisionistas EXTERNOS por tipo, cobros
        que no comisionan (sin aplicar / sin vendedor), incidencias y detalle
        al final. Quien no es Administrador de Comisiones SOLO recibe sus
        propios movimientos. Los datos contables (folio de factura, fecha de
        pago) se extraen con sudo porque el vendedor no tiene permisos de
        contabilidad."""
        user = self.env.user
        is_auth = user.has_group('om_advanced_commission.group_commission_manager')
        today = fields.Date.context_today(self)
        start = self._commission_start_date()
        try:
            year, mon = [int(x) for x in (month or '').split('-')]
        except (ValueError, AttributeError):
            year, mon = today.year, today.month
        # Nunca antes del mes de inicio de comisiones.
        if (year, mon) < (start.year, start.month):
            year, mon = start.year, start.month
        first = date(year, mon, 1)
        last = date(year, mon, monthrange(year, mon)[1])
        first = max(first, start)

        base_domain = [
            ('state', '!=', 'cancel'),
            ('pre_start', '=', False),
            ('company_id', 'in', self.env.companies.ids),
            ('partner_id.commission_excluded', '=', False),   # nunca figuran
        ] + self._commission_period_domain(first, last)
        domain = list(base_domain)
        if not is_auth:
            domain.append(('partner_id.user_ids', 'in', [user.id]))
        elif partner_id:
            domain.append(('partner_id', '=', int(partner_id)))

        scope = scope if scope in ('sellers', 'externals') else 'sellers'
        moves = self.search(domain, order='payment_date desc, id desc')
        all_moves = self.search(base_domain) if is_auth else moves

        company = self.env.company
        company_cur = company.currency_id

        def to_company(move, amount):
            cur = move.currency_id
            if not cur or cur == company_cur:
                return amount or 0.0
            return cur._convert(amount or 0.0, company_cur, move.company_id or company, move.date or today)

        def fmt_dt(value):
            return format_date(self.env, value, date_format='dd MMM yyyy') if value else ''

        Partner = self.env['res.partner'].sudo()
        seller_partners = Partner
        if is_auth:
            try:
                seller_partners = Partner.search(self.env['sale.order']._commission_seller_domain(), order='name')
            except Exception:  # noqa: BLE001
                seller_partners = Partner
        seller_ids = set(seller_partners.ids)

        legacy_role = {}

        def resolved_role(m):
            if m.rule_role:
                return m.rule_role
            key = (m.sale_order_id.id, m.partner_id.id)
            if key not in legacy_role:
                role = False
                if m.sale_order_id:
                    rule = m.sale_order_id.sudo().commission_rule_ids.filtered(
                        lambda r: r.partner_id == m.partner_id)[:1]
                    role = rule.role_type if rule else False
                if not role:
                    role = 'internal' if m.partner_id.id in seller_ids else 'other'
                legacy_role[key] = role
            return legacy_role[key]

        def is_internal(m):
            return resolved_role(m) == 'internal'

        if is_auth:
            want_internal = scope == 'sellers'
            moves = moves.filtered(lambda m: is_internal(m) == want_internal)
            all_moves = all_moves.filtered(lambda m: is_internal(m) == want_internal)

        def new_bucket():
            return {'total': 0.0, 'draft': 0.0, 'settled': 0.0, 'invoiced': 0.0,
                    'retained': 0.0, 'base': 0.0, 'gross': 0.0, 'calc_base': 0.0,
                    'deducted': 0.0, 'count': 0, 'orders': set(), 'seen': set()}

        def feed(bucket, m):
            amt = to_company(m, m.amount)
            bucket['total'] += amt
            if m.state in ('draft', 'settled', 'invoiced'):
                bucket[m.state] += amt
            if m.retention_factor and 0 < m.retention_factor < 1.0:
                bucket['retained'] += amt / m.retention_factor - amt
            bucket['count'] += 1
            if m.sale_order_id:
                bucket['orders'].add(m.sale_order_id.id)
            if not m.origin_move_id:
                # La base es la misma para todas las reglas del mismo cobro:
                # se cuenta una vez por conciliación (no por beneficiario).
                key = m.partial_reconcile_id.id or m.id
                if key not in bucket['seen']:
                    bucket['seen'].add(key)
                    bucket['base'] += to_company(m, m.base_amount_paid)
                    bucket['gross'] += to_company(m, m.amount_paid_total)
                # Base de cálculo y externos descontados sí son por beneficiario.
                bucket['calc_base'] += to_company(m, m.commission_base)
                bucket['deducted'] += to_company(m, m.external_deducted)

        def pack(bucket):
            out = {k: round(v, 2) for k, v in bucket.items() if k in
                   ('total', 'draft', 'settled', 'invoiced', 'retained', 'base', 'gross', 'calc_base', 'deducted')}
            out['count'] = bucket['count']
            out['orders'] = len(bucket['orders'])
            out['pct'] = round(bucket['total'] / bucket['calc_base'] * 100.0, 2) if bucket['calc_base'] else (
                round(bucket['total'] / bucket['base'] * 100.0, 2) if bucket['base'] else 0.0)
            out['avg_order'] = round(bucket['total'] / len(bucket['orders']), 2) if bucket['orders'] else 0.0
            return out

        # ── KPIs del filtro activo + filas de detalle ──
        kpis_b = new_bucket()
        rows = []
        for move in moves:
            feed(kpis_b, move)
            sm = move.sudo()
            so = sm.sale_order_id
            inv = sm.invoice_id or sm.invoice_line_id.move_id
            rk = resolved_role(move)
            amount = round(to_company(move, move.amount), 2)
            calc_base = round(to_company(move, move.commission_base), 2)
            base = round(to_company(move, move.base_amount_paid), 2)
            gross = round(to_company(move, move.amount_paid_total), 2)
            deducted = round(to_company(move, move.external_deducted), 2)
            pct = (abs(amount) / abs(calc_base) * 100.0) if calc_base else (
                (abs(amount) / abs(base) * 100.0) if base else 0.0)
            if move.origin_move_id:
                formula = 'Ajuste sobre %s' % (move.origin_move_id.name or '')
            elif move.rule_base == 'manual':
                formula = 'Monto fijo prorrateado al cobro'
            else:
                parts = []
                if gross:
                    parts.append('Cobrado %s%s' % (company_cur.symbol or '$', '{:,.2f}'.format(gross)))
                parts.append('sin IVA %s%s' % (company_cur.symbol or '$', '{:,.2f}'.format(base)))
                if not move.includes_services and rk != 'internal':
                    parts.append('solo bienes')
                if deducted:
                    parts.append('menos externos %s%s' % (company_cur.symbol or '$', '{:,.2f}'.format(deducted)))
                parts.append('base %s%s × %s%% = %s%s' % (
                    company_cur.symbol or '$', '{:,.2f}'.format(calc_base or base),
                    ('%.2f' % (move.rule_percent or pct)).rstrip('0').rstrip('.'),
                    company_cur.symbol or '$', '{:,.2f}'.format(amount)))
                formula = ' · '.join(parts)
            rows.append({
                'id': move.id,
                'name': move.name,
                'partner': move.partner_id.display_name,
                'partner_id': move.partner_id.id,
                'role': self.ROLE_SINGULAR.get(rk, 'Otro'),
                'order_id': so.id if so else False,
                'order': so.name if so else '',
                'order_date': fmt_dt(so.date_order) if so else '',
                'customer': so.partner_id.display_name if so and so.partner_id else '',
                'invoice': (inv.name or '') if inv else '',
                'payment_date': fmt_dt(move.payment_date or move.date),
                'gross': gross,
                'base': base,
                'calc_base': calc_base,
                'deducted': deducted,
                'pct': round(pct, 2),
                'amount': amount,
                'formula': formula,
                'services': bool(move.includes_services),
                'state': move.state,
                'is_refund': move.is_refund or move.is_reversal,
                'kind': ('reversal' if move.adjustment_kind == 'reversal'
                         else 'adjustment' if move.adjustment_kind == 'adjustment'
                         else 'refund' if move.is_refund else ''),
                'retained': bool(move.retention_factor and 0 < move.retention_factor < 1.0),
                'external_paid': bool(move.external_paid),
                'can_mark': is_auth and move.state in ('draft', 'settled'),
            })
        kpis = pack(kpis_b)

        # ── Secciones (solo administradores, sobre TODO el periodo) ──
        sellers, externals, incidents = [], [], {'count': 0, 'rows': []}
        people, role_summary = [], []
        blocked_ids = set()
        totals = {'sellers': 0.0, 'externals': 0.0}
        unapplied = {'count': 0, 'total': 0.0, 'rows': []}
        uncommissioned = {'count': 0, 'total': 0.0, 'rows': []}
        if is_auth:
            per_person = {}
            per_role = {}
            for m in all_moves:
                rk = resolved_role(m)
                entry = per_person.setdefault(m.partner_id.id, {'role': rk, 'bucket': new_bucket()})
                feed(entry['bucket'], m)
                feed(per_role.setdefault(rk, new_bucket()), m)
            for pid, entry in per_person.items():
                b = entry['bucket']
                if not b['count']:
                    continue
                partner = Partner.browse(pid)
                rk = entry['role']
                people.append({'id': pid, 'name': partner.display_name,
                               'user_login': partner.user_ids[:1].login or '',
                               'role': rk, 'role_label': self.ROLE_SINGULAR.get(rk, 'Otro'),
                               'inactive': rk == 'internal' and pid not in seller_ids, **pack(b)})
            people.sort(key=lambda x: (-x['total'], x['name']))
            sellers = people
            for rk, b in sorted(per_role.items(), key=lambda kv: -kv[1]['total']):
                role_summary.append({'role': rk, 'label': self.ROLE_LABELS.get(rk, 'Otros') if rk != 'internal' else 'Vendedores',
                                     'people': len([p for p in people if p['role'] == rk]), **pack(b)})
            key = 'sellers' if scope == 'sellers' else 'externals'
            totals[key] = round(sum(x['total'] for x in people), 2)

            Incident = self.env['commission.incident']
            try:
                Incident.sudo()._detect_receipt_mismatches()
            except Exception:  # noqa: BLE001
                pass
            open_inc = Incident.sudo().search([('state', '=', 'open'), ('company_id', 'in', self.env.companies.ids)])
            incidents = {
                'count': len(open_inc),
                'rows': [{
                    'id': i.id, 'name': i.name,
                    'kind': dict(i._fields['kind'].selection)[i.kind],
                    'severity': i.severity,
                    'partner': i.partner_id.display_name or '',
                    'payment': i.payment_id.name or i.payment_move_id.name or '',
                    'expected': round(i.amount_expected, 2), 'actual': round(i.amount_actual, 2),
                    'ratio': i.ratio,
                } for i in open_inc[:8]],
            }
            blocked_ids = set(Incident._blocked_commission_moves(moves).ids)
            if scope == 'sellers':
                unapplied = self.sudo()._commission_unapplied_payments(first, last)
                uncommissioned = self.sudo()._commission_uncommissioned_partials(first, last)
        for r in rows:
            r['incident'] = r['id'] in blocked_ids

        # ── Tendencia 6 meses (por fecha de cobro, desde el inicio) ──
        trend = []
        short = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
        y, m = year, mon
        months = []
        for _i in range(6):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        for (ty, tm) in reversed(months):
            before = (ty, tm) < (start.year, start.month)
            total = 0.0
            if not before:
                f = max(date(ty, tm, 1), start)
                l = date(ty, tm, monthrange(ty, tm)[1])
                dom = [('state', '!=', 'cancel'), ('pre_start', '=', False),
                       ('company_id', 'in', self.env.companies.ids),
                       ('partner_id.commission_excluded', '=', False)]
                dom += self._commission_period_domain(f, l)
                if not is_auth:
                    dom.append(('partner_id.user_ids', 'in', [user.id]))
                elif partner_id:
                    dom.append(('partner_id', '=', int(partner_id)))
                for mv in self.search(dom):
                    if is_auth and is_internal(mv) != (scope == 'sellers'):
                        continue
                    total += to_company(mv, mv.amount)
            trend.append({'key': '%04d-%02d' % (ty, tm), 'label': '%s %s' % (short[tm - 1], str(ty)[2:]),
                          'total': round(total, 2), 'current': (ty, tm) == (year, mon),
                          'before_start': before})

        month_names = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
                       'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
        month_label = '%s %s' % (month_names[mon - 1], year)
        service_roles = self.env['sale.order']._commission_service_roles()
        role_names = {'internal': 'vendedores', 'architect': 'embajadores',
                      'construction': 'constructoras', 'referrer': 'referidores'}
        with_services = [role_names[r] for r in ('internal', 'architect', 'construction', 'referrer') if r in service_roles]
        rules_help = (
            'Comisiones sobre lo COBRADO en %s, sin IVA. Comisionan también sobre servicios (fletes, manejo, corte): %s; '
            'el resto solo sobre bienes. Los vendedores comisionan sobre su base menos las comisiones externas del mismo cobro.'
            % (month_label, ', '.join(with_services) if with_services else 'nadie'))
        selected = Partner.browse(int(partner_id)).display_name if (is_auth and partner_id) else ''
        return {
            'is_authorizer': is_auth,
            'scope': scope,
            'basis': 'payment',
            'basis_help': rules_help,
            'start_date': fmt_dt(start),
            'start_month': '%04d-%02d' % (start.year, start.month),
            'is_start_month': (year, mon) == (start.year, start.month),
            'month': '%04d-%02d' % (year, mon),
            'month_label': month_label,
            'is_current_month': (year, mon) == (today.year, today.month),
            'currency_symbol': company_cur.symbol or '$',
            'kpis': kpis,
            'totals': totals,
            'rows': rows,
            'sellers': sellers,
            'people': people,
            'role_summary': role_summary,
            'externals': externals,
            'selected_partner': selected,
            'incidents': incidents,
            'unapplied': unapplied,
            'uncommissioned': uncommissioned,
            'trend': trend,
            'updated_at': format_date(self.env, today, date_format='dd MMM yyyy'),
        }

