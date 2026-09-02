from odoo import models, fields, api

from .sale_order import SERVICE_ROLES_PARAM, SERVICE_ROLES_DEFAULT, COMMISSION_ROLES


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    commission_product_id = fields.Many2one(
        'product.product',
        string='Producto para Comisiones',
        config_parameter='om_advanced_commission.default_commission_product_id',
        help='Producto de servicio usado al generar facturas de proveedor para comisionistas.'
    )
    # Por compañía (res.company.commission_journal_id); antes era parámetro global.
    commission_journal_id = fields.Many2one(
        related='company_id.commission_journal_id', readonly=False,
        string='Diario de Comisiones',
    )
    commission_seller_max_percent = fields.Float(
        string='% Máx. Vendedores sin Autorización',
        config_parameter='om_advanced_commission.seller_max_percent',
        default=2.5,
        help='Tope del total de vendedores internos por orden. Aplica a órdenes nuevas; '
             'las existentes conservan su permitido.')
    commission_base_on_cash_received = fields.Boolean(
        string='Comisión sobre pesos efectivamente recibidos',
        config_parameter='om_advanced_commission.base_on_cash_received',
        default=True,
        help='Facturas en divisa: la base son los pesos que entraron al tipo de cambio del pago. '
             'Desactivado = pesos facturados al tipo de cambio de la factura.')
    commission_incident_abs_tolerance = fields.Float(
        string='Tolerancia de saldo a favor (monto)',
        config_parameter='om_advanced_commission.incident_abs_tolerance', default=1000.0,
        help='Saldo a favor permitido sin incidencia, en moneda de la compañía.')
    commission_incident_pct_tolerance = fields.Float(
        string='Tolerancia de saldo a favor (%)',
        config_parameter='om_advanced_commission.incident_pct_tolerance', default=10.0,
        help='Porcentaje sobre lo adeudado/aplicado. Se usa el mayor entre monto y porcentaje.')
    commission_external_max_percent = fields.Float(
        string='% Máx. Externo sin Autorización',
        config_parameter='om_advanced_commission.external_max_percent',
        default=0.0,
        help='Tope de comisiones externas (embajador/constructora/referidor) como % del subtotal. 0 = sin tope.')

    # ── Inicio de comisiones (corte no retroactivo, por fecha de cobro) ──
    commission_start_date = fields.Date(
        string='Inicio de comisiones',
        config_parameter='om_advanced_commission.start_date',
        help='Solo comisionan los cobros recibidos a partir de esta fecha. Lo cobrado antes '
             'se considera pagado fuera del sistema: no aparece pendiente, en el panel ni en '
             'reportes, y no genera asientos.')

    # ── Cobros siempre aplicados a factura ──
    commission_auto_apply_payments = fields.Boolean(
        string='Aplicar cobros a facturas automáticamente',
        config_parameter='om_advanced_commission.auto_apply_payments',
        help='Al registrar un pago de cliente o publicar una factura, el saldo sin aplicar se '
             'aplica solo a las facturas abiertas del cliente (misma moneda; primero la del memo, '
             'luego las más antiguas). Un pago sin aplicar no comisiona.')

    # ── Quién comisiona sobre SERVICIOS (fletes, manejo de materiales, corte…) ──
    commission_services_internal = fields.Boolean(
        string='Vendedores',
        help='Los vendedores internos comisionan también sobre fletes y servicios.')
    commission_services_architect = fields.Boolean(
        string='Embajadores',
        help='Los embajadores comisionan también sobre fletes y servicios.')
    commission_services_construction = fields.Boolean(
        string='Constructoras',
        help='Las constructoras comisionan también sobre fletes y servicios.')
    commission_services_referrer = fields.Boolean(
        string='Referidores',
        help='Los referidores comisionan también sobre fletes y servicios.')

    _SERVICE_FIELDS = {
        'internal': 'commission_services_internal',
        'architect': 'commission_services_architect',
        'construction': 'commission_services_construction',
        'referrer': 'commission_services_referrer',
    }

    def get_values(self):
        res = super().get_values()
        roles = self.env['sale.order']._commission_service_roles()
        for role, fname in self._SERVICE_FIELDS.items():
            res[fname] = role in roles
        return res

    def set_values(self):
        super().set_values()
        roles = [role for role in COMMISSION_ROLES if self[self._SERVICE_FIELDS[role]]]
        # Cadena vacía = nadie comisiona sobre servicios (set_param con False
        # borraría el parámetro y regresaría al default).
        self.env['ir.config_parameter'].sudo().set_param(SERVICE_ROLES_PARAM, ','.join(roles) or ' ')

    def action_commission_apply_payments_now(self):
        """Aplica ahora todos los cobros con saldo sin aplicar."""
        self.ensure_one()
        self.set_values()
        applied = self.env['account.payment']._som_auto_apply_all()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': 'Cobros aplicados',
                'message': '%d aplicación(es) de cobros a facturas. Lo que no encontró factura queda '
                           'como anticipo y se aplica al facturar.' % len(applied),
                'type': 'success', 'sticky': False,
            },
        }

    def action_commission_apply_start_date(self):
        """Cierra lo pendiente con cobro anterior al inicio (idempotente)."""
        self.ensure_one()
        self.set_values()
        closed = self.env['commission.move']._commission_apply_start_date()
        start = self.env['commission.move']._commission_start_date()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': 'Inicio de comisiones',
                'message': '%d comisión(es) con cobro anterior al %s quedaron como pagadas fuera del sistema.'
                           % (len(closed), fields.Date.to_string(start)),
                'type': 'success', 'sticky': False,
            },
        }
