from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError


class SaleCommissionRule(models.Model):
    _name = 'sale.commission.rule'
    _description = 'Regla de Comisión en Ventas'

    sale_order_id = fields.Many2one('sale.order', ondelete='cascade', index=True)
    # Línea de la orden: hereda su compañía (NULL = regla sin orden, compartida).
    company_id = fields.Many2one(
        'res.company', string='Compañía', related='sale_order_id.company_id',
        store=True, readonly=True, index=True)
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)

    role_type = fields.Selection([
        ('internal', 'Vendedor'),
        ('architect', 'Embajador'),
        ('construction', 'Constructora'),
        ('referrer', 'Referidor')
    ], string='Rol', required=True, default='internal')

    calculation_base = fields.Selection([
        ('amount_untaxed', 'Monto Base (Subtotal)'),
        ('amount_total', 'Monto Total (Inc. Impuestos)'),
        ('margin', 'Margen (Ganancia)'),
        ('gross_utility', 'Utilidad Bruta (Subtotal - Comisiones Externas)'),
        ('manual', 'Manual / Fijo')
    ], string='Base de Cálculo', default='amount_untaxed', required=True)

    percent = fields.Float(string='Porcentaje %')
    fixed_amount = fields.Monetary(string='Monto Fijo', currency_field='currency_id')

    # Posición del vendedor interno (1..3) que originó esta regla: permite
    # sincronizar EN SITIO (sin destruir/recrear) cuando cambian los campos
    # seller*_id / seller*_percent de la orden, conservando id, autorización
    # e historial.
    seller_slot = fields.Integer(string='Posición Vendedor', readonly=True, default=0)

    # Estimado si se cobrara el 100% de la orden, YA con los factores de
    # retención (lo que realmente se pagaría hoy). raw = sin retención.
    estimated_amount = fields.Monetary(compute='_compute_estimated', string='Estimado Total')
    raw_estimated_amount = fields.Monetary(compute='_compute_estimated', string='Estimado sin retención')
    effective_percent = fields.Float(compute='_compute_estimated', string='% Efectivo',
                                     help='Porcentaje que realmente se aplica hoy (tras retención por autorización pendiente).')
    currency_id = fields.Many2one(related='sale_order_id.currency_id')

    requires_authorization = fields.Boolean(string='Requiere Autorización', default=False, readonly=True)
    authorization_id = fields.Many2one('commission.authorization', string='Autorización', readonly=True)

    # ------------------------------------------------------------------
    # Candado "Otras comisiones": solo EXTERNOS y nunca duplicar a un vendedor
    # ------------------------------------------------------------------
    @api.model
    def _partner_is_internal_user(self, partner):
        return bool(partner and partner.sudo().user_ids.filtered(lambda u: u.active and not u.share))

    @api.constrains('partner_id', 'role_type', 'sale_order_id')
    def _check_external_rule(self):
        for rule in self:
            so = rule.sale_order_id
            # 1) El rol Vendedor solo nace de Vendedor 1/2/3. La regla interna
            #    la crea el sistema (sync) y al guardar desde el formulario
            #    puede llegar sin contexto ni seller_slot (campo readonly):
            #    es legítima siempre que el beneficiario SEA un vendedor de
            #    la orden. Solo se bloquea si alguien mete rol Vendedor a mano
            #    para un contacto que no está arriba.
            if rule.role_type == 'internal':
                sellers = (so.seller1_id | so.seller2_id | so.seller3_id) if so else self.env['res.partner']
                if self.env.context.get('commission_sync') or rule.seller_slot or rule.partner_id in sellers:
                    continue
                raise ValidationError(
                    "Los vendedores se capturan arriba, en Vendedor 1 / 2 / 3. "
                    "En 'Otras comisiones' solo van embajadores, constructoras o referidores.")
            # 2) Un usuario interno de Odoo no puede ir como comisionista externo.
            if self._partner_is_internal_user(rule.partner_id):
                raise ValidationError(
                    "%s es usuario interno de Odoo: no puede registrarse en 'Otras comisiones'. "
                    "Si debe comisionar, va como Vendedor 1 / 2 / 3 (tope del %s%%)."
                    % (rule.partner_id.display_name, so._commission_seller_max() if so else 2.5))
            # 3) Nadie arriba y abajo a la vez.
            if so and rule.partner_id in (so.seller1_id | so.seller2_id | so.seller3_id):
                raise ValidationError(
                    "%s ya está como vendedor en esta orden; no puede llevar además una comisión externa."
                    % rule.partner_id.display_name)

    # ------------------------------------------------------------------
    # Cálculo
    # ------------------------------------------------------------------
    def _commission_amount(self, bases, fixed_converted, external_total):
        """Monto de esta regla para un conjunto de bases (todas en la misma
        moneda). `bases` = dict con untaxed / total / margin / manual_share.
        `fixed_converted` = monto fijo ya en la moneda destino.
        `external_total` = comisiones externas ya calculadas en esa moneda
        (solo lo usa gross_utility)."""
        self.ensure_one()
        pct = (self.percent or 0.0) / 100.0
        base = self.calculation_base
        if base == 'manual':
            return (fixed_converted or 0.0) * (bases.get('manual_share') or 0.0)
        if base == 'amount_untaxed':
            return (bases.get('untaxed') or 0.0) * pct
        if base == 'amount_total':
            return (bases.get('total') or 0.0) * pct
        if base == 'margin':
            return (bases.get('margin') or 0.0) * pct
        if base == 'gross_utility':
            return ((bases.get('untaxed') or 0.0) - (external_total or 0.0)) * pct
        return 0.0

    def _commission_base_value(self, bases, external_total):
        """Base (en dinero) sobre la que se aplicó el % de esta regla; 0 para
        monto fijo. Se guarda en el movimiento para que cada monto se explique
        solo: base × % = comisión."""
        self.ensure_one()
        base = self.calculation_base
        if base == 'amount_untaxed':
            return bases.get('untaxed') or 0.0
        if base == 'amount_total':
            return bases.get('total') or 0.0
        if base == 'margin':
            return bases.get('margin') or 0.0
        if base == 'gross_utility':
            return (bases.get('untaxed') or 0.0) - (external_total or 0.0)
        return 0.0

    @api.depends('percent', 'fixed_amount', 'calculation_base', 'role_type', 'partner_id',
                 'sale_order_id.amount_untaxed', 'sale_order_id.amount_total',
                 'sale_order_id.order_line.price_subtotal',
                 'sale_order_id.order_line.price_total',
                 'sale_order_id.order_line.no_commission',
                 'sale_order_id.commission_rule_ids.percent',
                 'sale_order_id.commission_rule_ids.fixed_amount',
                 'sale_order_id.commission_rule_ids.calculation_base',
                 'sale_order_id.commission_rule_ids.role_type',
                 'sale_order_id.seller1_percent', 'sale_order_id.seller2_percent',
                 'sale_order_id.seller3_percent',
                 'sale_order_id.commission_allowed_seller_percent',
                 'sale_order_id.commission_allowed_external_percent')
    def _compute_estimated(self):
        for so in self.mapped('sale_order_id'):
            paid_lines = so._commission_full_order_lines()
            raw = so._commission_amounts_for_payment(paid_lines, so.currency_id, apply_factors=False)
            eff = so._commission_amounts_for_payment(paid_lines, so.currency_id, apply_factors=True)
            subtotal = sum(pl[1] for pl in paid_lines if not (pl[0] and pl[0].no_commission))
            for rule in self.filtered(lambda r: r.sale_order_id == so):
                rule.raw_estimated_amount = raw.get(rule, 0.0)
                rule.estimated_amount = eff.get(rule, 0.0)
                rule.effective_percent = (
                    rule.estimated_amount / subtotal * 100.0 if subtotal else 0.0)
        for rule in self.filtered(lambda r: not r.sale_order_id):
            rule.raw_estimated_amount = rule.estimated_amount = 0.0
            rule.effective_percent = 0.0

    # ------------------------------------------------------------------
    # Congelación tras confirmar + bitácora en el chatter de la orden
    # ------------------------------------------------------------------
    def _commission_check_locked(self, orders):
        """Sin candado: las reglas se pueden editar en cualquier estado (el
        ayudante de un vendedor debe poder cambiar el comisionista). El
        control es la bitácora en el chatter y el realineo en tiempo real."""
        return

    def _commission_describe(self):
        self.ensure_one()
        role = dict(self._fields['role_type'].selection).get(self.role_type, self.role_type)
        base = dict(self._fields['calculation_base'].selection).get(self.calculation_base, '')
        if self.calculation_base == 'manual':
            val = '%s fijo' % (self.fixed_amount or 0.0)
        else:
            val = '%s %%' % (self.percent or 0.0)
        return '%s · %s · %s · %s' % (role, self.partner_id.display_name, base, val)

    def _commission_log(self, action, details=None):
        if self.env.context.get('commission_sync'):
            return
        for rule in self.filtered(lambda r: r.sale_order_id and isinstance(r.sale_order_id.id, int)):
            body = '%s regla de comisión: %s' % (action, details or rule._commission_describe())
            rule.sale_order_id.sudo().message_post(body=body, message_type='notification')

    def _commission_refresh_orders(self, orders):
        if self.env.context.get('commission_no_refresh'):
            return
        orders.filtered(lambda s: isinstance(s.id, int))._commission_refresh()

    def _commission_zero_out(self, reason):
        """La regla deja de aplicar (se borra o cambia de beneficiario): sus
        movimientos quedan en cero neto en tiempo real."""
        Move = self.env['commission.move'].sudo()
        moves = Move.search([('rule_id', 'in', self.ids), ('origin_move_id', '=', False),
                             ('state', '!=', 'cancel')])
        moves._commission_neutralize('adjustment', reason)

    @api.model_create_multi
    def create(self, vals_list):
        orders = self.env['sale.order'].browse(
            [v['sale_order_id'] for v in vals_list if v.get('sale_order_id')])
        self._commission_check_locked(orders)
        rules = super().create(vals_list)
        rules._commission_log('Agregada')
        self._commission_refresh_orders(rules.mapped('sale_order_id'))
        return rules

    def write(self, vals):
        tracked = {'partner_id', 'role_type', 'calculation_base', 'percent', 'fixed_amount'}
        if tracked & set(vals):
            self._commission_check_locked(self.mapped('sale_order_id'))
            before = {r.id: r._commission_describe() for r in self}
            if vals.get('partner_id'):
                changing = self.filtered(lambda r: r.partner_id.id != vals['partner_id'])
                changing._commission_zero_out('la regla cambió de beneficiario')
            res = super().write(vals)
            for rule in self:
                after = rule._commission_describe()
                if before.get(rule.id) != after:
                    rule._commission_log('Modificada', '%s  →  %s' % (before.get(rule.id), after))
            self._commission_refresh_orders(self.mapped('sale_order_id'))
            return res
        return super().write(vals)

    def unlink(self):
        orders = self.mapped('sale_order_id')
        self._commission_check_locked(orders)
        self._commission_log('Eliminada')
        self.filtered(lambda r: r.sale_order_id.state in ('sale', 'done'))._commission_zero_out(
            'la regla fue eliminada')
        res = super().unlink()
        self._commission_refresh_orders(orders)
        return res
