from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)

# Tope por defecto de vendedores internos sin autorización; configurable en
# Ajustes (om_advanced_commission.seller_max_percent).
SELLER_MAX_PCT = 2.5
SELLER_FIELDS = ('seller1_id', 'seller2_id', 'seller3_id',
                 'seller1_percent', 'seller2_percent', 'seller3_percent')


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')
    x_project_id = fields.Many2one('project.project', string='Proyecto (Job Name)')

    # Vendedores internos = SOLO contactos que son usuarios de Odoo con
    # permisos de ventas (no cualquier contacto). El dominio filtra el
    # desplegable y _check_sellers lo garantiza en servidor.
    seller1_id = fields.Many2one('res.partner', string='Vendedor 1', tracking=True,
                                 domain=lambda self: self._commission_seller_domain())
    seller1_percent = fields.Float(string='% Vendedor 1', default=2.5, tracking=True)
    seller2_id = fields.Many2one('res.partner', string='Vendedor 2', tracking=True,
                                 domain=lambda self: self._commission_seller_domain())
    seller2_percent = fields.Float(string='% Vendedor 2', default=0.0, tracking=True)
    seller3_id = fields.Many2one('res.partner', string='Vendedor 3', tracking=True,
                                 domain=lambda self: self._commission_seller_domain())
    seller3_percent = fields.Float(string='% Vendedor 3', default=0.0, tracking=True)

    total_seller_percent = fields.Float(
        string='% Total Vendedores', compute='_compute_total_seller_percent', store=True)
    total_commission_percent = fields.Float(
        string='% Total Comisionado', compute='_compute_total_commission_percent', store=True)
    total_external_percent = fields.Float(
        string='% Externo (equivalente s/ subtotal)', compute='_compute_total_external_percent', store=True,
        help='Suma de comisiones externas (embajador, constructora, referidor) expresada como % del subtotal comisionable.')

    # ── Gate de autorización ───────────────────────────────────────────
    # Los porcentajes capturados NUNCA se reescriben. Lo que se autoriza es
    # el % PERMITIDO por orden; si lo capturado lo excede, el cálculo aplica
    # un factor proporcional (retención) hasta que se apruebe.
    commission_allowed_seller_percent = fields.Float(
        string='% Vendedores Permitido', readonly=True, copy=False,
        default=lambda self: self._commission_seller_max(),
        help='Máximo total de vendedores que se paga sin retención. Sube al aprobar una autorización.')
    commission_allowed_external_percent = fields.Float(
        string='% Externo Permitido', readonly=True, copy=False, default=0.0,
        help='Máximo externo autorizado para esta orden (0 = aplica el tope general de Ajustes).')
    commission_seller_effective_percent = fields.Float(
        string='% Vendedores Efectivo', compute='_compute_commission_requires_auth', store=True)
    commission_requires_auth = fields.Boolean(
        string='Requiere Autorización', compute='_compute_commission_requires_auth', store=True)
    commission_authorization_id = fields.Many2one(
        'commission.authorization', string='Autorización Vigente', readonly=True, copy=False)

    # ── Trazabilidad ────────────────────────────────────────────────────
    commission_move_ids = fields.One2many('commission.move', 'sale_order_id', string='Movimientos de Comisión')
    commission_move_count = fields.Integer(compute='_compute_commission_stats')
    commission_total_amount = fields.Monetary(
        compute='_compute_commission_stats', string='Comisión Devengada',
        currency_field='company_currency_id')
    commission_paid_base = fields.Monetary(
        compute='_compute_commission_stats', string='Base Cobrada',
        currency_field='company_currency_id')
    company_currency_id = fields.Many2one(related='company_id.currency_id')

    # ------------------------------------------------------------------
    # Vendedores internos: usuarios con permisos de ventas
    # ------------------------------------------------------------------
    @api.model
    def _commission_user_groups_field(self):
        """Odoo 19: all_group_ids (directos + implicados); antes groups_id."""
        Users = self.env['res.users']
        for fname in ('all_group_ids', 'group_ids', 'groups_id'):
            if fname in Users._fields:
                return fname
        return 'groups_id'

    @api.model
    def _commission_seller_domain(self):
        group = self.env.ref('sales_team.group_sale_salesman', raise_if_not_found=False)
        if not group:
            return [('user_ids', '!=', False)]
        return [
            ('user_ids.active', '=', True),
            ('user_ids.share', '=', False),
            ('user_ids.%s' % self._commission_user_groups_field(), 'in', [group.id]),
        ]

    @api.model
    def _commission_is_sales_user_partner(self, partner):
        if not partner:
            return True
        group = self.env.ref('sales_team.group_sale_salesman', raise_if_not_found=False)
        gfield = self._commission_user_groups_field()
        users = partner.sudo().user_ids.filtered(lambda u: u.active and not u.share)
        if not users:
            return False
        if not group:
            return True
        return any(group in u[gfield] for u in users)

    @api.constrains('seller1_id', 'seller2_id', 'seller3_id')
    def _check_sellers(self):
        for so in self:
            chosen = [p for p in (so.seller1_id, so.seller2_id, so.seller3_id) if p]
            if len(chosen) != len({c.id for c in chosen}):
                raise ValidationError("Un vendedor no puede ocupar dos posiciones en la misma orden.")
            dup = so.commission_rule_ids.filtered(lambda r: r.role_type != 'internal' and r.partner_id in chosen)
            if dup:
                raise ValidationError(
                    "%s ya tiene una comisión en 'Otras comisiones'; no puede ir también como vendedor."
                    % ', '.join(dup.mapped('partner_id.display_name')))
            bad = [p.display_name for p in (so.seller1_id, so.seller2_id, so.seller3_id)
                   if p and not self._commission_is_sales_user_partner(p)]
            if bad:
                raise ValidationError(
                    "Solo usuarios de Odoo con permisos de ventas pueden ser vendedores internos: %s. "
                    "Para embajadores, constructoras o referidores usa 'Otras Comisiones'."
                    % ', '.join(bad))

    # ------------------------------------------------------------------
    # Parámetros
    # ------------------------------------------------------------------
    @api.model
    def _commission_seller_max(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'om_advanced_commission.seller_max_percent')
        try:
            val = float(raw) if raw not in (None, '', False) else SELLER_MAX_PCT
        except (TypeError, ValueError):
            val = SELLER_MAX_PCT
        return val if val > 0 else SELLER_MAX_PCT

    @api.model
    def _commission_external_max(self):
        """0 = sin tope externo (comportamiento histórico)."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'om_advanced_commission.external_max_percent')
        try:
            return max(float(raw or 0.0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Create / write
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('seller1_percent'):
                vals.setdefault('seller1_percent', 2.5)
            if not vals.get('commission_allowed_seller_percent'):
                vals['commission_allowed_seller_percent'] = self._commission_seller_max()
        res = super().create(vals_list)
        for so in res:
            upd = {}
            if not so.seller1_id and so.user_id and so.user_id.partner_id \
                    and not so.user_id.partner_id.commission_excluded:
                upd['seller1_id'] = so.user_id.partner_id.id
            # Unificación con sale_order_second_salesperson (user_id_2): el
            # co-vendedor de la primera pantalla es el Vendedor 2 por defecto.
            u2 = so._commission_user2_partner()
            if u2 and not so.seller2_id:
                upd['seller2_id'] = u2.id
            if upd:
                so.with_context(commission_sync=True).write(upd)
            # Garantiza reglas internas (cubre duplicados: el O2M no se copia).
            so.with_context(commission_sync=True)._sync_seller_rules()
        return res

    def _commission_user2_partner(self):
        self.ensure_one()
        if 'user_id_2' in self._fields and self.user_id_2:
            return self.user_id_2.partner_id
        return self.env['res.partner']

    @api.onchange('user_id')
    def _onchange_user_id_seller(self):
        if self.user_id and self.user_id.partner_id and not self.seller1_id \
                and not self.user_id.partner_id.commission_excluded:
            self.seller1_id = self.user_id.partner_id

    def write(self, vals):
        seller_changed = bool(set(SELLER_FIELDS) & set(vals))
        # Sin candado por estado: cualquier vendedor (p. ej. el ayudante de
        # otro) puede cambiar el comisionista de una orden confirmada. El
        # cambio queda en el chatter (tracking) y las comisiones ya
        # devengadas se realinean en tiempo real.
        # Las reglas escritas vía comandos del O2M no refrescan una por una:
        # se refresca UNA vez al final.
        res = super(SaleOrder, self.with_context(commission_no_refresh=True)).write(vals)
        if 'user_id_2' in vals and vals.get('user_id_2'):
            for so in self.filtered(lambda s: not s.seller2_id and s.state in ('draft', 'sent')):
                u2 = so._commission_user2_partner()
                if u2:
                    so.with_context(commission_sync=True).write({'seller2_id': u2.id})
        if seller_changed:
            self.with_context(commission_no_refresh=True)._sync_seller_rules()
        lines_touched = 'order_line' in vals and 'no_commission' in repr(vals.get('order_line'))
        if (seller_changed or 'commission_rule_ids' in vals or lines_touched) \
                and not self.env.context.get('commission_no_refresh'):
            self._commission_refresh()
        return res

    # ------------------------------------------------------------------
    # Tiempo real: cualquier cambio de reglas/autorización se refleja YA
    # ------------------------------------------------------------------
    def _commission_refresh(self):
        """Alinea los movimientos de la orden con lo que HOY dan sus reglas y
        su % permitido. No es un paso de proceso: lo disparan los propios
        cambios (reglas, vendedores, líneas excluidas, autorizaciones)."""
        Partial = self.env['account.partial.reconcile'].sudo()
        for so in self.filtered(lambda s: isinstance(s.id, int) and s.state in ('sale', 'done')):
            invoices = so.sudo().invoice_ids.filtered(lambda i: i.state == 'posted')
            recv = invoices.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
            if not recv:
                continue
            partials = Partial.search([
                '|', ('debit_move_id', 'in', recv.ids), ('credit_move_id', 'in', recv.ids)])
            partials._create_commission_moves(refresh=True)

    # ------------------------------------------------------------------
    # Sincronización vendedores ↔ reglas internas (EN SITIO)
    # ------------------------------------------------------------------
    def _seller_slots(self):
        self.ensure_one()
        return [
            (1, self.seller1_id, self.seller1_percent),
            (2, self.seller2_id, self.seller2_percent),
            (3, self.seller3_id, self.seller3_percent),
        ]

    def _sync_seller_rules(self):
        """Alinea las reglas internas con seller*_id/seller*_percent sin
        destruirlas: misma regla (mismo id) mientras exista la posición, así se
        conservan autorización, historial y snapshots de movimientos."""
        Rule = self.env['sale.commission.rule'].with_context(commission_sync=True)
        for so in self:
            in_onchange = not isinstance(so.id, int)  # id virtual (onchange) vs persistido; NewId cambió de ruta en Odoo 19
            internal = so.commission_rule_ids.filtered(lambda r: r.role_type == 'internal')
            used = Rule.browse()
            for slot, partner, pct in so._seller_slots():
                existing = internal.filtered(lambda r: r.seller_slot == slot and r not in used)[:1]
                if not existing and partner:
                    existing = internal.filtered(lambda r: r.partner_id == partner and r not in used)[:1]
                if partner and pct:
                    vals = {
                        'partner_id': partner.id,
                        'role_type': 'internal',
                        'calculation_base': 'gross_utility',
                        'percent': pct,
                        'seller_slot': slot,
                    }
                    if existing:
                        used |= existing
                        current = {
                            'partner_id': existing.partner_id.id,
                            'role_type': existing.role_type,
                            'calculation_base': existing.calculation_base,
                            'percent': existing.percent,
                            'seller_slot': existing.seller_slot,
                        }
                        if current != vals:
                            if in_onchange:
                                existing.update(vals)
                            else:
                                existing.with_context(commission_sync=True).write(vals)
                    else:
                        if in_onchange:
                            so.commission_rule_ids |= Rule.new(vals)
                        else:
                            vals['sale_order_id'] = so.id
                            used |= Rule.create(vals)
            stale = internal - used
            if stale:
                if in_onchange:
                    so.commission_rule_ids -= stale
                else:
                    stale.with_context(commission_sync=True).unlink()

    # ------------------------------------------------------------------
    # Totales / gate
    # ------------------------------------------------------------------
    @api.depends('seller1_percent', 'seller2_percent', 'seller3_percent')
    def _compute_total_seller_percent(self):
        for so in self:
            so.total_seller_percent = (
                (so.seller1_percent or 0.0) +
                (so.seller2_percent or 0.0) +
                (so.seller3_percent or 0.0)
            )

    @api.depends('total_seller_percent', 'total_external_percent')
    def _compute_total_commission_percent(self):
        for so in self:
            so.total_commission_percent = so.total_seller_percent + so.total_external_percent

    @api.depends('commission_rule_ids.percent', 'commission_rule_ids.fixed_amount',
                 'commission_rule_ids.calculation_base', 'commission_rule_ids.role_type',
                 'order_line.price_subtotal', 'order_line.price_total', 'order_line.no_commission')
    def _compute_total_external_percent(self):
        for so in self:
            paid_lines = so._commission_full_order_lines()
            subtotal = sum(pl[1] for pl in paid_lines if not (pl[0] and pl[0].no_commission))
            if not subtotal:
                so.total_external_percent = 0.0
                continue
            raw = so._commission_amounts_for_payment(paid_lines, so.currency_id, apply_factors=False)
            ext = sum(v for r, v in raw.items() if r.role_type != 'internal')
            so.total_external_percent = round(ext / subtotal * 100.0, 4)

    @api.depends('total_seller_percent', 'commission_allowed_seller_percent',
                 'total_external_percent', 'commission_allowed_external_percent')
    def _compute_commission_requires_auth(self):
        for so in self:
            seller_factor, ext_factor = so._commission_factors()
            so.commission_seller_effective_percent = so.total_seller_percent * seller_factor
            so.commission_requires_auth = seller_factor < 1.0 or ext_factor < 1.0

    def _commission_factors(self):
        """(factor_vendedores, factor_externo) ∈ (0, 1]. 1 = sin retención."""
        self.ensure_one()
        eps = 1e-6
        total_seller = self.total_seller_percent or 0.0
        allowed_seller = self.commission_allowed_seller_percent or self._commission_seller_max()
        seller_factor = 1.0
        if total_seller > allowed_seller + eps and total_seller > 0:
            seller_factor = allowed_seller / total_seller

        ext_factor = 1.0
        cap = self._commission_external_max()
        if cap > 0:
            allowed_ext = max(cap, self.commission_allowed_external_percent or 0.0)
            total_ext = self.total_external_percent or 0.0
            if total_ext > allowed_ext + eps and total_ext > 0:
                ext_factor = allowed_ext / total_ext
        return seller_factor, ext_factor

    def _has_approved_auth(self):
        """Compatibilidad: ¿lo capturado ya está cubierto por lo permitido?"""
        self.ensure_one()
        seller_factor, ext_factor = self._commission_factors()
        return seller_factor >= 1.0 and ext_factor >= 1.0

    @api.onchange('seller1_id', 'seller2_id', 'seller3_id',
                  'seller1_percent', 'seller2_percent', 'seller3_percent')
    def _onchange_sellers(self):
        self._sync_seller_rules()
        excluded = [p.display_name for p in (self.seller1_id, self.seller2_id, self.seller3_id)
                    if p and p.commission_excluded]
        if excluded:
            return {'warning': {
                'title': 'Sin comisión',
                'message': '%s está marcado como excluido de comisiones: puede figurar como vendedor, '
                           'pero no generará comisión.' % ', '.join(excluded),
            }}
        total = (self.seller1_percent or 0) + (self.seller2_percent or 0) + (self.seller3_percent or 0)
        allowed = self.commission_allowed_seller_percent or self._commission_seller_max()
        if total > allowed + 1e-6:
            return {
                'warning': {
                    'title': 'Autorización Requerida',
                    'message': (
                        f"El porcentaje total de vendedores ({total}%) supera el permitido "
                        f"({allowed}%). Se pagará solo hasta {allowed}% hasta que un autorizador apruebe el excedente."
                    )
                }
            }

    # ------------------------------------------------------------------
    # Motor de cálculo compartido (estimado en la orden y devengo por cobro)
    # ------------------------------------------------------------------
    def _commission_product_lines(self):
        self.ensure_one()
        return self.order_line.filtered(lambda l: not l.display_type)

    def _commission_full_order_lines(self):
        """paid_lines al 100% de la orden, en moneda de la orden."""
        self.ensure_one()
        return [(l, l.price_subtotal, l.price_total) for l in self._commission_product_lines()]

    @api.model
    def _commission_line_margin_ratio(self, line):
        """Margen/subtotal de una línea de venta. Usa sale_margin si está;
        si no, costo ALL-IN (x_costo_mayor, MXN) o standard_price."""
        if not line or not line.price_subtotal:
            return 0.0
        try:
            if 'margin' in line._fields:
                return max((line.margin or 0.0) / line.price_subtotal, 0.0)
            product = line.product_id
            if not product:
                return 0.0
            cost = 0.0
            if 'x_costo_mayor' in product._fields:
                cost = product.x_costo_mayor or 0.0
            if not cost:
                cost = product.standard_price or 0.0
            company = line.company_id or line.order_id.company_id
            cost_total = cost * (line.product_uom_qty or 0.0)
            if line.currency_id and company.currency_id and line.currency_id != company.currency_id:
                cost_total = company.currency_id._convert(
                    cost_total, line.currency_id, company,
                    line.order_id.date_order or fields.Date.today())
            return max((line.price_subtotal - cost_total) / line.price_subtotal, 0.0)
        except Exception:  # jamás tumbar un cálculo de comisión por el costo
            return 0.0

    def _commission_amounts_for_payment(self, paid_lines, to_currency, apply_factors=True, date=None):
        """Monto por regla para un conjunto de líneas cobradas.

        paid_lines: lista de (sale_line | None, base_sin_iva, base_con_iva),
        montos ya en `to_currency`. Devuelve {regla: monto (positivo)}.
        Es el ÚNICO lugar donde se aplica la fórmula: el estimado de la orden
        y el devengo por conciliación pasan por aquí."""
        self.ensure_one()
        company = self.company_id
        date = date or self.date_order or fields.Date.today()
        commissionable = [pl for pl in paid_lines if not (pl[0] and pl[0].no_commission)]
        bases = {
            'untaxed': sum(pl[1] for pl in commissionable),
            'total': sum(pl[2] for pl in commissionable),
            'margin': sum(pl[1] * self._commission_line_margin_ratio(pl[0]) for pl in commissionable),
        }
        # Monto fijo: se prorratea según la parte de la orden que cubre este cobro.
        all_untaxed = sum(pl[1] for pl in paid_lines)
        order_untaxed = self.amount_untaxed or 0.0
        if to_currency and self.currency_id and to_currency != self.currency_id:
            order_untaxed = self.currency_id._convert(order_untaxed, to_currency, company, date)
        bases['manual_share'] = (all_untaxed / order_untaxed) if order_untaxed else 0.0

        seller_factor, ext_factor = self._commission_factors() if apply_factors else (1.0, 1.0)

        def fixed_in(rule):
            amt = rule.fixed_amount or 0.0
            if to_currency and self.currency_id and to_currency != self.currency_id:
                amt = self.currency_id._convert(amt, to_currency, company, date)
            return amt

        result = {}
        external_total = 0.0
        # Excluidos de comisiones (p. ej. dueños que venden): sus reglas no
        # producen movimientos nuevos ni participan en la utilidad bruta.
        # Como no entran en `result`, el refresh tampoco toca lo que ya
        # tuvieran devengado (la exclusión aplica solo hacia adelante).
        rules = self.commission_rule_ids.filtered(lambda r: not r.partner_id.commission_excluded)
        for rule in rules.filtered(lambda r: r.role_type != 'internal'):
            amt = rule._commission_amount(bases, fixed_in(rule), 0.0) * ext_factor
            result[rule] = amt
            external_total += amt
        for rule in rules.filtered(lambda r: r.role_type == 'internal'):
            result[rule] = rule._commission_amount(bases, fixed_in(rule), external_total) * seller_factor
        return result

    # ------------------------------------------------------------------
    # Autorización
    # ------------------------------------------------------------------
    def action_request_commission_auth(self):
        self.ensure_one()
        seller_factor, ext_factor = self._commission_factors()
        if seller_factor < 1.0:
            auth_type, requested, current = 'seller', self.total_seller_percent, self.commission_allowed_seller_percent
        elif ext_factor < 1.0:
            auth_type, requested = 'external', self.total_external_percent
            current = max(self._commission_external_max(), self.commission_allowed_external_percent or 0.0)
        else:
            return self._return_notification("Esta orden no requiere autorización de comisiones.", "info")
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'commission.authorization',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'default_auth_type': auth_type,
                'default_requested_percent': requested,
                'default_current_percent': current,
                'default_requested_by': self.env.user.id,
            }
        }

    # ------------------------------------------------------------------
    # Recalculo manual (repone solo lo que sigue en borrador)
    # ------------------------------------------------------------------
    def action_recalc_commissions(self):
        """Fuerza la alineación (normalmente innecesaria: es automática)."""
        self.ensure_one()
        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")
        before = len(self.commission_move_ids)
        self._commission_refresh()
        self.invalidate_recordset(['commission_move_ids'])
        after = len(self.commission_move_ids)
        return self._return_notification(
            "Comisiones alineadas con las reglas vigentes (%d movimiento(s), %+d)." % (after, after - before),
            "success")

    # ------------------------------------------------------------------
    # Smart button
    # ------------------------------------------------------------------
    @api.depends('commission_move_ids.amount', 'commission_move_ids.state',
                 'commission_move_ids.base_amount_paid')
    def _compute_commission_stats(self):
        for so in self:
            moves = so.commission_move_ids.filtered(lambda m: m.state != 'cancel')
            so.commission_move_count = len(moves)
            so.commission_total_amount = sum(moves.mapped('amount'))
            # La base cobrada es la misma para todas las reglas de un cobro:
            # se toma una vez por conciliación (no por beneficiario).
            seen, base = set(), 0.0
            for m in moves.filtered(lambda m: not m.origin_move_id):
                key = m.partial_reconcile_id.id or m.id
                if key in seen:
                    continue
                seen.add(key)
                base += m.base_amount_paid or 0.0
            so.commission_paid_base = base

    def action_view_commission_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Comisiones de %s' % self.name,
            'res_model': 'commission.move',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }

    def _return_notification(self, message, type='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'Gestión de Comisiones', 'message': message, 'type': type, 'sticky': False}
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')

    def write(self, vals):
        res = super().write(vals)
        if 'no_commission' in vals and not self.env.context.get('commission_no_refresh'):
            self.mapped('order_id')._commission_refresh()
        return res
