## ./__init__.py
```py
from . import models
from . import wizard
from . import report
```

## ./__manifest__.py
```py
{
    'name': 'Gestión Avanzada de Comisiones (Cash Basis & Proyectos)',
    'version': '19.0.1.1.0',
    'category': 'Sales/Commissions',
    'summary': 'Motor de comisiones multi-agente basado en pagos, margenes y liquidaciones.',
    'author': 'Alphaqueb Consulting',
    'depends': ['sale_management', 'account', 'purchase', 'project'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'views/res_config_settings_views.xml',
        'views/sale_order_views.xml',
        'views/commission_move_views.xml',
        'views/commission_settlement_views.xml',
        'views/commission_authorization_views.xml',
        'wizard/commission_make_invoice_views.xml',
        'wizard/commission_report_wizard_views.xml',
        'wizard/commission_authorization_reject_wizard_views.xml',
        'report/commission_report_template.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}```

## ./data/sequence_data.xml
```xml
<odoo>
    <record id="seq_commission_move" model="ir.sequence">
        <field name="name">Commission Move</field>
        <field name="code">commission.move</field>
        <field name="prefix">COMM/</field>
        <field name="padding">5</field>
    </record>
</odoo>
```

## ./models/__init__.py
```py
from . import res_config_settings
from . import commission_rule
from . import commission_move
from . import commission_settlement
from . import commission_authorization
from . import sale_order
from . import account_move```

## ./models/account_move.py
```py
from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        """
        Crea commission.move por cada partial reconcile.
        Solo procesa partials que involucren líneas receivable de facturas de cliente.
        """
        CommissionMove = self.env['commission.move'].sudo()

        for rec in self:
            try:
                # --- 1. Identificar factura y pago ---
                # Determinar cuál move line es de la factura
                debit_move = rec.debit_move_id
                credit_move = rec.credit_move_id

                invoice = debit_move.move_id
                payment = credit_move.move_id

                if invoice.move_type not in ('out_invoice', 'out_refund'):
                    invoice, payment = payment, invoice
                    debit_move, credit_move = credit_move, debit_move

                if invoice.move_type not in ('out_invoice', 'out_refund'):
                    continue

                # --- FILTRO CLAVE: Solo procesar partials de líneas receivable ---
                # La línea de la factura en el partial debe ser receivable
                invoice_line = debit_move if debit_move.move_id == invoice else credit_move
                if invoice_line.account_id.account_type != 'asset_receivable':
                    continue

                is_refund = invoice.move_type == 'out_refund'
                invoice_origin = invoice.reversed_entry_id if is_refund and invoice.reversed_entry_id else invoice

                if invoice_origin.move_type not in ('out_invoice', 'out_refund'):
                    continue

                company = invoice_origin.company_id
                company_currency = company.currency_id

                # --- 2. Monto reconciliado en moneda de la factura ---
                invoice_currency = invoice_origin.currency_id
                if invoice_currency == company_currency:
                    amount_reconciled_inv_currency = rec.amount
                else:
                    # Usar amount_currency de la línea de la factura
                    if invoice_line == debit_move:
                        amount_reconciled_inv_currency = (
                            abs(rec.debit_amount_currency)
                            if hasattr(rec, 'debit_amount_currency') and rec.debit_amount_currency
                            else rec.amount
                        )
                    else:
                        amount_reconciled_inv_currency = (
                            abs(rec.credit_amount_currency)
                            if hasattr(rec, 'credit_amount_currency') and rec.credit_amount_currency
                            else rec.amount
                        )

                invoice_total = invoice_origin.amount_total
                if invoice_total == 0:
                    continue

                payment_ratio = min(amount_reconciled_inv_currency / invoice_total, 1.0)

                invoice_untaxed_mxn = abs(invoice_origin.amount_untaxed_signed)
                paid_base_mxn = invoice_untaxed_mxn * payment_ratio

                # --- 3. SOs relacionadas ---
                sale_lines = invoice_origin.invoice_line_ids.mapped('sale_line_ids')
                sale_orders = sale_lines.mapped('order_id').filtered(lambda so: so.commission_rule_ids)

                if not sale_orders:
                    fallback_sos = self.env['sale.order'].search([
                        ('invoice_ids', 'in', invoice_origin.id)
                    ]).filtered(lambda so: so.commission_rule_ids)
                    if not fallback_sos:
                        continue
                    sale_orders = fallback_sos

                # --- 4. Peso de cada SO dentro de la factura ---
                so_weights = {}
                total_weight = 0.0
                for so in sale_orders:
                    so_inv_lines = invoice_origin.invoice_line_ids.filtered(
                        lambda l, _so=so: l.sale_line_ids & _so.order_line
                    )
                    weight = sum(abs(l.balance) for l in so_inv_lines) if so_inv_lines else 0.0

                    if weight == 0.0:
                        weight = so.currency_id._convert(
                            so.amount_total, company_currency, company,
                            so.date_order or fields.Date.today()
                        )

                    so_weights[so.id] = weight
                    total_weight += weight

                if total_weight == 0:
                    continue

                # --- 5. Payment real ---
                payment_rec = self.env['account.payment'].search(
                    [('move_id', '=', payment.id)], limit=1
                )

                sign = -1 if is_refund else 1

                # --- 6. Crear commission.move por SO y regla ---
                for so in sale_orders:
                    so_ratio = so_weights[so.id] / total_weight
                    so_paid_base = paid_base_mxn * so_ratio

                    so_inv_lines = invoice_origin.invoice_line_ids.filtered(
                        lambda l, _so=so: l.sale_line_ids & _so.order_line
                    )
                    best_inv_line = so_inv_lines[:1] if so_inv_lines else invoice_origin.invoice_line_ids[:1]

                    for rule in so.commission_rule_ids:
                        if CommissionMove.search_count([
                            ('partial_reconcile_id', '=', rec.id),
                            ('partner_id', '=', rule.partner_id.id),
                            ('sale_order_id', '=', so.id),
                        ], limit=1):
                            continue

                        if rule.calculation_base == 'manual':
                            rule_amount_mxn = rule.currency_id._convert(
                                rule.fixed_amount, company_currency, company,
                                so.date_order or fields.Date.today()
                            )
                        else:
                            rule_amount_mxn = so.currency_id._convert(
                                rule.estimated_amount, company_currency, company,
                                so.date_order or fields.Date.today()
                            )

                        so_total_mxn = so.currency_id._convert(
                            so.amount_total, company_currency, company,
                            so.date_order or fields.Date.today()
                        )
                        if so_total_mxn == 0:
                            continue

                        paid_total_mxn_so = abs(invoice_origin.amount_total_signed) * payment_ratio * so_ratio
                        final_ratio = paid_total_mxn_so / so_total_mxn
                        commission_amount = rule_amount_mxn * final_ratio * sign

                        if abs(commission_amount) < 0.01:
                            continue

                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': so.id,
                            'invoice_line_id': best_inv_line.id if best_inv_line else False,
                            'payment_id': payment_rec.id if payment_rec else False,
                            'partial_reconcile_id': rec.id,
                            'company_id': company.id,
                            'amount': commission_amount,
                            'base_amount_paid': so_paid_base * sign,
                            'currency_id': company_currency.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {invoice.name} / {so.name} ({round(final_ratio * 100, 1)}%)",
                        })

            except Exception as e:
                _logger.error(f"[COMMISSION] Error en partial {rec.id}: {e}", exc_info=True)

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        res.sudo()._create_commission_moves()
        return res```

## ./models/commission_authorization.py
```py
from odoo import models, fields, api
from odoo.exceptions import UserError


class CommissionAuthorization(models.Model):
    _name = 'commission.authorization'
    _description = 'Solicitud de Autorización de Comisión Extra'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Referencia', readonly=True, default='Nueva Solicitud')
    sale_order_id = fields.Many2one('sale.order', string='Orden de Venta', required=True, readonly=True,
                                    states={'draft': [('readonly', False)]})
    requested_by = fields.Many2one('res.users', string='Solicitado por',
                                   default=lambda self: self.env.user, readonly=True)
    authorizer_id = fields.Many2one('res.users', string='Autorizador',
                                    domain=lambda self: [('groups_id', 'in', [
                                        self.env.ref('om_advanced_commission.group_commission_authorizer').id
                                    ])])
    requested_percent = fields.Float(string='% Solicitado (Total Vendedores)', required=True)
    current_percent = fields.Float(string='% Actual Permitido', default=2.5, readonly=True)
    justification = fields.Text(string='Justificación')
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('pending', 'Pendiente'),
        ('approved', 'Aprobado'),
        ('rejected', 'Rechazado'),
    ], default='draft', tracking=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    reject_reason = fields.Text(string='Motivo de Rechazo', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'Nueva Solicitud') == 'Nueva Solicitud':
                so = self.env['sale.order'].browse(vals.get('sale_order_id'))
                vals['name'] = f"AUTH-{so.name or 'nuevo'}"
        return super().create(vals_list)

    def action_submit(self):
        self.write({'state': 'pending'})
        # Notificar al autorizador si está definido
        if self.authorizer_id:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=self.authorizer_id.id,
                note=f"Solicitud de autorización de comisión extra para {self.sale_order_id.name}: {self.requested_percent}%"
            )

    def action_approve(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_authorizer'):
            raise UserError("No tienes permisos para autorizar comisiones.")
        self.write({'state': 'approved'})
        # Notificar al solicitante
        self.message_post(body=f"✅ Autorización aprobada por {self.env.user.name}")

    def action_reject(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_authorizer'):
            raise UserError("No tienes permisos para rechazar autorizaciones.")
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'commission.authorization.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_authorization_id': self.id},
        }

    def action_reset_draft(self):
        self.write({'state': 'draft'})```

## ./models/commission_move.py
```py
from odoo import models, fields, api


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

    _sql_constraints = [
        ('unique_commission_per_reconcile_partner_rule',
         'UNIQUE(partial_reconcile_id, partner_id, sale_order_id)',
         'Ya existe una comisión para esta conciliación, comisionista y orden de venta.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('commission.move') or 'COMM'
        return super().create(vals_list)```

## ./models/commission_rule.py
```py
from odoo import models, fields, api
from odoo.exceptions import ValidationError


class SaleCommissionRule(models.Model):
    _name = 'sale.commission.rule'
    _description = 'Regla de Comisión en Ventas'

    sale_order_id = fields.Many2one('sale.order', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)

    role_type = fields.Selection([
        ('internal', 'Vendedor'),
        ('architect', 'Arquitecto'),
        ('construction', 'Constructora'),
        ('referrer', 'Referidor')
    ], string='Rol', required=True, default='internal')

    calculation_base = fields.Selection([
        ('amount_untaxed', 'Monto Base (Subtotal)'),
        ('amount_total', 'Monto Total (Inc. Impuestos)'),
        ('margin', 'Margen (Ganancia)'),
        ('manual', 'Manual / Fijo')
    ], string='Base de Cálculo', default='amount_untaxed', required=True)

    percent = fields.Float(string='Porcentaje %')
    fixed_amount = fields.Monetary(string='Monto Fijo', currency_field='currency_id')

    estimated_amount = fields.Monetary(compute='_compute_estimated', string='Estimado Total')
    currency_id = fields.Many2one(related='sale_order_id.currency_id')

    requires_authorization = fields.Boolean(string='Requiere Autorización', default=False, readonly=True)
    authorization_id = fields.Many2one('commission.authorization', string='Autorización', readonly=True)

    @api.depends('percent', 'fixed_amount', 'calculation_base',
                 'sale_order_id.amount_untaxed', 'sale_order_id.amount_total',
                 'sale_order_id.order_line.price_subtotal')
    def _compute_estimated(self):
        for rule in self:
            so = rule.sale_order_id
            amount = 0.0
            lines = so.order_line.filtered(lambda l: not getattr(l, 'no_commission', False))

            if rule.calculation_base == 'manual':
                amount = rule.fixed_amount
            elif rule.calculation_base == 'amount_untaxed':
                base = sum(lines.mapped('price_subtotal'))
                amount = base * (rule.percent / 100.0)
            elif rule.calculation_base == 'amount_total':
                base = sum(lines.mapped('price_total'))
                amount = base * (rule.percent / 100.0)
            elif rule.calculation_base == 'margin':
                base = 0.0
                try:
                    if lines and 'margin' in lines[0]._fields:
                        base = sum(lines.mapped('margin'))
                    elif 'margin' in so._fields:
                        base = so.margin
                except (AttributeError, KeyError):
                    base = 0.0
                amount = base * (rule.percent / 100.0)

            rule.estimated_amount = amount```

## ./models/commission_settlement.py
```py
from odoo import models, fields, api
from odoo.exceptions import ValidationError

class CommissionSettlement(models.Model):
    _name = 'commission.settlement'
    _description = 'Hoja de Liquidación de Comisiones'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Referencia', default='Borrador', copy=False)
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)
    company_id = fields.Many2one('res.company', string='Compañía', required=True, default=lambda self: self.env.company)
    date = fields.Date(string='Fecha Corte', default=fields.Date.context_today)
    
    move_ids = fields.One2many('commission.move', 'settlement_id', string='Movimientos')
    
    total_amount = fields.Monetary(compute='_compute_totals', string='Total a Pagar', store=True)
    currency_id = fields.Many2one('res.currency', required=True, default=lambda self: self.env.company.currency_id)
    
    vendor_bill_id = fields.Many2one('account.move', string='Factura Proveedor Generada')
    
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('approved', 'Aprobado'),
        ('invoiced', 'Facturado'),
        ('cancel', 'Cancelado')
    ], default='draft', tracking=True)

    @api.depends('move_ids.amount')
    def _compute_totals(self):
        for rec in self:
            rec.total_amount = sum(rec.move_ids.mapped('amount'))

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_create_bill(self):
        self.ensure_one()
        
        if self.vendor_bill_id:
            raise ValidationError("Ya existe una factura de proveedor para esta liquidación.")
        
        param_obj = self.env['ir.config_parameter'].sudo()
        prod_id_str = param_obj.get_param('om_advanced_commission.default_commission_product_id')
        journal_id_str = param_obj.get_param('om_advanced_commission.default_commission_journal_id')

        if not prod_id_str or not journal_id_str:
            raise ValidationError("Falta configuración. Ve a Ajustes > Ventas > Configuración Comisiones.")

        try:
            product_id = int(prod_id_str)
            journal_id = int(journal_id_str)
        except (ValueError, TypeError):
            raise ValidationError("Configuración de comisiones corrupta. Revisa producto y diario en Ajustes.")

        product = self.env['product.product'].browse(product_id).exists()
        journal = self.env['account.journal'].browse(journal_id).exists()
        
        if not product or not journal:
            raise ValidationError("El producto o diario configurado ya no existe.")
        
        if journal.company_id != self.company_id:
            raise ValidationError(f"El diario {journal.name} no pertenece a la compañía {self.company_id.name}.")

        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id,
            'company_id': self.company_id.id,
            'invoice_date': fields.Date.today(),
            'journal_id': journal_id,
            'currency_id': self.currency_id.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': product_id,
                'name': f"Liquidación Comisiones Ref: {self.name}",
                'quantity': 1,
                'price_unit': self.total_amount,
            })]
        })
        self.vendor_bill_id = bill.id
        self.state = 'invoiced'
        self.move_ids.write({'state': 'invoiced'})
        
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': bill.id,
            'view_mode': 'form',
        }```

## ./models/res_config_settings.py
```py
from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    commission_product_id = fields.Many2one(
        'product.product', 
        string='Producto para Comisiones',
        config_parameter='om_advanced_commission.default_commission_product_id',
        help='Producto de servicio usado al generar facturas de proveedor para comisionistas.'
    )
    commission_journal_id = fields.Many2one(
        'account.journal',
        string='Diario de Comisiones',
        config_parameter='om_advanced_commission.default_commission_journal_id',
        domain=[('type', '=', 'purchase')]
    )
```

## ./models/sale_order.py
```py
from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

SELLER_MAX_PCT = 2.5


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')
    x_project_id = fields.Many2one('project.project', string='Proyecto (Job Name)')

    seller1_id = fields.Many2one('res.partner', string='Vendedor 1', domain=[('is_company', '=', False)])
    seller1_percent = fields.Float(string='% Vendedor 1', default=0.0)
    seller2_id = fields.Many2one('res.partner', string='Vendedor 2', domain=[('is_company', '=', False)])
    seller2_percent = fields.Float(string='% Vendedor 2', default=0.0)
    seller3_id = fields.Many2one('res.partner', string='Vendedor 3', domain=[('is_company', '=', False)])
    seller3_percent = fields.Float(string='% Vendedor 3', default=0.0)

    total_seller_percent = fields.Float(
        string='% Total Vendedores', compute='_compute_total_seller_percent', store=True)
    total_commission_percent = fields.Float(
        string='% Total Comisionado', compute='_compute_total_commission_percent', store=True)
    commission_requires_auth = fields.Boolean(
        string='Requiere Autorización', compute='_compute_commission_requires_auth', store=True)
    commission_authorization_id = fields.Many2one(
        'commission.authorization', string='Autorización Vigente', readonly=True)

    @api.depends('seller1_percent', 'seller2_percent', 'seller3_percent')
    def _compute_total_seller_percent(self):
        for so in self:
            so.total_seller_percent = (
                (so.seller1_percent or 0.0) +
                (so.seller2_percent or 0.0) +
                (so.seller3_percent or 0.0)
            )

    @api.depends('commission_rule_ids.percent', 'commission_rule_ids.calculation_base',
                 'seller1_percent', 'seller2_percent', 'seller3_percent')
    def _compute_total_commission_percent(self):
        for so in self:
            seller_pct = so.total_seller_percent
            other_pct = sum(
                r.percent for r in so.commission_rule_ids
                if r.role_type != 'internal' and r.calculation_base != 'manual'
            )
            so.total_commission_percent = seller_pct + other_pct

    @api.depends('total_seller_percent', 'commission_authorization_id',
                 'commission_authorization_id.state')
    def _compute_commission_requires_auth(self):
        for so in self:
            if so.total_seller_percent <= SELLER_MAX_PCT:
                so.commission_requires_auth = False
                continue
            # Buscar en BD directamente, no solo en el campo
            auth_ok = so._has_approved_auth()
            so.commission_requires_auth = not auth_ok

    def _has_approved_auth(self):
        """Verifica en BD si existe autorización aprobada para esta SO."""
        return bool(self.env['commission.authorization'].sudo().search_count([
            ('sale_order_id', '=', self.id),
            ('state', '=', 'approved'),
        ], limit=1))

    @api.onchange('seller1_id', 'seller2_id', 'seller3_id',
                  'seller1_percent', 'seller2_percent', 'seller3_percent')
    def _onchange_sellers(self):
        self._sync_seller_rules()
        total = (self.seller1_percent or 0) + (self.seller2_percent or 0) + (self.seller3_percent or 0)
        if total > SELLER_MAX_PCT:
            return {
                'warning': {
                    'title': 'Autorización Requerida',
                    'message': (
                        f"El porcentaje total de vendedores ({total}%) supera el límite de "
                        f"{SELLER_MAX_PCT}%. Necesitas solicitar autorización antes de recalcular comisiones."
                    )
                }
            }

    def _sync_seller_rules(self):
        non_internal_cmds = []
        for rule in self.commission_rule_ids:
            if rule.role_type != 'internal':
                if rule.id and isinstance(rule.id, int):
                    non_internal_cmds.append((4, rule.id))

        new_internal_cmds = []
        for partner, pct in [
            (self.seller1_id, self.seller1_percent),
            (self.seller2_id, self.seller2_percent),
            (self.seller3_id, self.seller3_percent),
        ]:
            if partner and pct:
                new_internal_cmds.append((0, 0, {
                    'partner_id': partner.id,
                    'role_type': 'internal',
                    'calculation_base': 'amount_untaxed',
                    'percent': pct,
                }))

        self.commission_rule_ids = [(5, 0, 0)] + new_internal_cmds + non_internal_cmds

    def write(self, vals):
        res = super().write(vals)
        seller_fields = {'seller1_id', 'seller2_id', 'seller3_id',
                         'seller1_percent', 'seller2_percent', 'seller3_percent'}
        if seller_fields & set(vals.keys()):
            for so in self:
                old_internal = so.commission_rule_ids.filtered(lambda r: r.role_type == 'internal')
                old_internal.unlink()
                for partner, pct in [
                    (so.seller1_id, so.seller1_percent),
                    (so.seller2_id, so.seller2_percent),
                    (so.seller3_id, so.seller3_percent),
                ]:
                    if partner and pct:
                        self.env['sale.commission.rule'].create({
                            'sale_order_id': so.id,
                            'partner_id': partner.id,
                            'role_type': 'internal',
                            'calculation_base': 'amount_untaxed',
                            'percent': pct,
                        })
        return res

    def action_request_commission_auth(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'commission.authorization',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'default_requested_percent': self.total_seller_percent,
                'default_current_percent': SELLER_MAX_PCT,
                'default_requested_by': self.env.user.id,
            }
        }

    def action_recalc_commissions(self):
        self.ensure_one()

        # Verificar directamente en BD, ignorando caché
        if self.total_seller_percent > SELLER_MAX_PCT:
            if not self._has_approved_auth():
                raise UserError(
                    f"El porcentaje total de vendedores ({self.total_seller_percent}%) supera el límite de "
                    f"{SELLER_MAX_PCT}%. Obtén una autorización aprobada antes de recalcular."
                )

        CommissionMove = self.env['commission.move']

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        invoices = self.invoice_ids.filtered(
            lambda x: x.state == 'posted' and x.payment_state != 'not_paid'
        )
        if not invoices:
            return self._return_notification("Sin facturas pagadas.", "warning")

        old_drafts = CommissionMove.search([('sale_order_id', '=', self.id), ('state', '=', 'draft')])
        old_drafts.unlink()

        created_count = 0
        for inv in invoices:
            receivable_lines = inv.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable'
            )
            partials = self.env['account.partial.reconcile'].search([
                '|',
                ('debit_move_id', 'in', receivable_lines.ids),
                ('credit_move_id', 'in', receivable_lines.ids),
            ])
            before_count = CommissionMove.search_count([('sale_order_id', '=', self.id)])
            partials._create_commission_moves()
            after_count = CommissionMove.search_count([('sale_order_id', '=', self.id)])
            created_count += (after_count - before_count)

        msg_type = "success" if created_count > 0 else "info"
        return self._return_notification(f"Recálculo finalizado. {created_count} comisiones creadas.", msg_type)

    def _return_notification(self, message, type='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'Gestión de Comisiones', 'message': message, 'type': type, 'sticky': False}
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')```

## ./report/__init__.py
```py
from . import commission_report```

## ./report/commission_report_template.xml
```xml
<odoo>
    <!-- 1. Definición de la Acción de Reporte -->
    <record id="action_report_commission_pdf" model="ir.actions.report">
        <field name="name">Reporte Detallado de Comisiones</field>
        <field name="model">commission.report.wizard</field>
        <field name="report_type">qweb-pdf</field>
        <field name="report_name">om_advanced_commission.report_commission_document</field>
        <field name="report_file">om_advanced_commission.report_commission_document</field>
        <field name="print_report_name">'Comisiones %s - %s' % (object.date_from, object.date_to)</field>
        <field name="binding_model_id" ref="model_commission_report_wizard"/>
        <field name="binding_type">report</field>
        <field name="paperformat_id" ref="base.paperformat_euro"/> 
    </record>

    <!-- 2. Plantilla QWeb Estilizada -->
    <template id="report_commission_document">
        <t t-call="web.html_container">
            <t t-call="web.external_layout">
                <div class="page" style="font-family: 'Helvetica', Arial, sans-serif; font-size: 11px; color: #333;">
                    
                    <!-- ENCABEZADO MODERNO -->
                    <div class="row mb-5">
                        <div class="col-12 text-center">
                            <h2 style="font-weight: 900; text-transform: uppercase; letter-spacing: 1px; color: #000; margin-bottom: 5px;">
                                Reporte de Comisiones
                            </h2>
                            <div style="border-bottom: 3px solid #000; width: 60px; margin: 0 auto 15px auto;"></div>
                            <p style="font-size: 13px; color: #666;">
                                <strong>Periodo:</strong> 
                                <span t-esc="data['date_from']" t-options='{"widget": "date"}' class="ml-1 mr-1"/>
                                <i class="fa fa-long-arrow-right mx-1"/>
                                <span t-esc="data['date_to']" t-options='{"widget": "date"}' class="ml-1"/>
                            </p>
                        </div>
                    </div>

                    <!-- Iterar sobre cada Vendedor -->
                    <t t-foreach="docs" t-as="group">
                        
                        <!-- SEPARADOR DE VENDEDOR -->
                        <div class="mt-4 mb-3" style="border-left: 6px solid #000; padding-left: 10px; background-color: #f8f9fa; padding-top:8px; padding-bottom:8px;">
                            <span style="font-size: 15px; font-weight: bold; color: #212529; text-transform: uppercase;">
                                <i class="fa fa-user-circle-o mr-2" style="opacity: 0.6;"></i>
                                <span t-esc="group['partner'].name"/>
                            </span>
                        </div>

                        <!-- TABLA MODERNA -->
                        <table class="table table-sm table-borderless" style="margin-bottom: 30px;">
                            <!-- 
                                CABECERA NEGRA, TEXTO BLANCO 
                                Nota: Usamos !important en el color para sobreescribir estilos de Bootstrap en PDF 
                            -->
                            <thead style="background-color: #000000; color: #FFFFFF;">
                                <tr style="border-bottom: 2px solid #000;">
                                    <th class="text-center align-middle"
                                        style="width: 8%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        FOLIO
                                    </th>
                                    <th class="text-center align-middle"
                                        style="width: 13%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        FECHA FACTURA
                                    </th>
                                    <th class="align-middle"
                                        style="width: 14%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        RAZÓN SOCIAL
                                    </th>
                                    <th class="align-middle"
                                        style="width: 10%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        JOB NAME
                                    </th>
                                    <th class="text-center align-middle"
                                        style="width: 11%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        FECHA PAGO
                                    </th>
                                    <th class="text-center align-middle"
                                        style="width: 9%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        FACTURA
                                    </th>
                                    <th class="text-right align-middle"
                                        style="width: 12%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        $ FACT. (SIN IVA)
                                    </th>
                                    <th class="text-center align-middle"
                                        style="width: 10%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        % COMISIÓN
                                    </th>
                                    <th class="text-right align-middle"
                                        style="width: 14%; padding: 8px; font-weight: 600; color: #FFFFFF !important; font-size: 9px;">
                                        MONTO COMISIÓN
                                    </th>
                                </tr>

                            </thead>
                            
                            <!-- CUERPO -->
                            <tbody>
                                <tr t-foreach="group['moves']" t-as="move" style="border-bottom: 1px solid #e9ecef;">
                                    <td class="text-center align-middle py-2">
                                        <span t-field="move.sale_order_id.name" style="font-weight: 500;"/>
                                    </td>
                                    <td class="text-center align-middle text-muted">
                                        <span t-if="move.invoice_line_id" t-field="move.invoice_line_id.move_id.invoice_date"/>
                                        <span t-else="">-</span>
                                    </td>
                                    <td class="align-middle">
                                        <span t-field="move.sale_order_id.partner_id.name" 
                                              style="display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px; font-weight: 500;"/>
                                    </td>
                                    <td class="align-middle text-muted">
                                        <span t-if="move.sale_order_id.x_project_id" t-field="move.sale_order_id.x_project_id.name"/>
                                        <span t-else="">-</span>
                                    </td>
                                    <td class="text-center align-middle">
                                        <span t-if="move.payment_id" t-field="move.payment_id.date"/>
                                        <span t-else="" t-field="move.date"/>
                                    </td>
                                    <td class="text-center align-middle">
                                        <span class="badge badge-light border" style="font-size: 10px;">
                                            <t t-if="move.invoice_line_id">
                                                <span t-field="move.invoice_line_id.move_id.name"/>
                                            </t>
                                            <t t-else="">Varios</t>
                                        </span>
                                    </td>
                                    <td class="text-right align-middle">
                                        <span t-field="move.base_amount_paid" 
                                              t-options='{"widget": "monetary", "display_currency": move.currency_id}'/>
                                    </td>
                                    <!-- Porcentaje limpio (sin fondo) -->
                                    <td class="text-center align-middle">
                                        <t t-if="move.base_amount_paid and move.base_amount_paid != 0">
                                            <span t-esc="abs(round((move.amount / move.base_amount_paid) * 100, 2))" style="font-weight: bold;"/>%
                                        </t>
                                        <t t-else="">Fijo</t>
                                    </td>
                                    <td class="text-right align-middle" style="font-weight: bold; color: #000;">
                                        <span t-field="move.amount" 
                                              t-options='{"widget": "monetary", "display_currency": move.currency_id}'/>
                                    </td>
                                </tr>
                            </tbody>

                            <!-- PIE DE TOTALES -->
                            <tfoot>
                                <tr style="background-color: #f0f0f0;">
                                    <td colspan="6" class="text-right align-middle text-uppercase" style="padding: 10px; font-weight: bold; color: #555;">
                                        Totales <span t-esc="group['partner'].name"/>:
                                    </td>
                                    <td class="text-right align-middle" style="padding: 10px; font-weight: bold;">
                                        <span t-esc="group['total_base']" 
                                              t-options='{"widget": "monetary", "display_currency": group["currency"]}'/>
                                    </td>
                                    <td class="align-middle"></td>
                                    <td class="text-right align-middle" style="padding: 10px; background-color: #1a1a1a; color: #fff; font-size: 12px; font-weight: bold;">
                                        <span t-esc="group['total_commission']" 
                                              t-options='{"widget": "monetary", "display_currency": group["currency"]}'/>
                                    </td>
                                </tr>
                            </tfoot>
                        </table>
                    </t>

                    <div t-if="not docs" class="alert alert-secondary mt-5 text-center p-5 shadow-sm">
                        <h4 class="alert-heading">Sin Información</h4>
                        <p>No se encontraron comisiones aprobadas o pendientes para este rango de fechas.</p>
                    </div>

                    <!-- NOTA LEGAL / DISCLAIMER -->
                    <div class="row" style="margin-top: 50px; page-break-inside: avoid;">
                        <div class="col-12">
                            <div style="border-top: 1px solid #ccc; padding-top: 10px; font-size: 9px; color: #666; text-align: justify;">
                                <strong>IMPORTANTE:</strong> Las comisiones detalladas en el presente reporte han sido calculadas sobre la base de los pagos efectivamente recibidos.
                                Se hace constar que aquellas comisiones previamente liquidadas que sean objeto de <strong>devoluciones, cancelaciones o notas de crédito</strong> posteriores
                                por parte del cliente, resultarán en un ajuste negativo (deducción) automático en el siguiente periodo de cálculo y liquidación de comisiones del agente correspondiente.
                            </div>
                        </div>
                    </div>

                </div>
            </t>
        </t>
    </template>
</odoo>```

## ./report/commission_report.py
```py
from odoo import models, api


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
            'company': self.env.company,
        }```

## ./security/security.xml
```xml
<odoo>
    <record id="group_commission_manager" model="res.groups">
        <field name="name">Administrador de Comisiones</field>
    </record>

    <record id="group_commission_authorizer" model="res.groups">
        <field name="name">Autorizador de Comisiones</field>
        <field name="implied_ids" eval="[(4, ref('om_advanced_commission.group_commission_manager'))]"/>
    </record>

    <!-- Multi-company rules -->
    <record id="commission_move_company_rule" model="ir.rule">
        <field name="name">Commission Move: multi-company</field>
        <field name="model_id" ref="model_commission_move"/>
        <field name="domain_force">[('company_id', 'in', company_ids)]</field>
    </record>

    <record id="commission_settlement_company_rule" model="ir.rule">
        <field name="name">Commission Settlement: multi-company</field>
        <field name="model_id" ref="model_commission_settlement"/>
        <field name="domain_force">[('company_id', 'in', company_ids)]</field>
    </record>

    <!-- Regla: vendedores solo ven sus propios commission.move -->
    <record id="commission_move_salesperson_rule" model="ir.rule">
        <field name="name">Commission Move: vendedor solo ve los suyos</field>
        <field name="model_id" ref="model_commission_move"/>
        <field name="domain_force">[('partner_id.user_ids', 'in', [user.id])]</field>
        <field name="groups" eval="[(4, ref('sales_team.group_sale_salesman'))]"/>
        <field name="perm_read" eval="True"/>
        <field name="perm_write" eval="False"/>
        <field name="perm_create" eval="False"/>
        <field name="perm_unlink" eval="False"/>
    </record>
</odoo>```

## ./views/commission_authorization_views.xml
```xml
<odoo>
    <record id="view_commission_authorization_form" model="ir.ui.view">
        <field name="name">commission.authorization.form</field>
        <field name="model">commission.authorization</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_submit" string="Solicitar Autorización" type="object"
                            invisible="state != 'draft'" class="btn-primary"/>
                    <button name="action_approve" string="Aprobar" type="object"
                            invisible="state != 'pending'" class="btn-success"
                            groups="om_advanced_commission.group_commission_authorizer"/>
                    <button name="action_reject" string="Rechazar" type="object"
                            invisible="state != 'pending'" class="btn-danger"
                            groups="om_advanced_commission.group_commission_authorizer"/>
                    <button name="action_reset_draft" string="Restablecer" type="object"
                            invisible="state != 'rejected'" class="btn-secondary"
                            groups="om_advanced_commission.group_commission_authorizer"/>
                    <field name="state" widget="statusbar"
                           statusbar_visible="draft,pending,approved,rejected"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1><field name="name" readonly="1"/></h1>
                    </div>
                    <group>
                        <group string="Solicitud">
                            <field name="sale_order_id" readonly="state != 'draft'"/>
                            <field name="requested_by" readonly="1"/>
                            <field name="authorizer_id"
                                   groups="om_advanced_commission.group_commission_authorizer"/>
                        </group>
                        <group string="Comisión">
                            <field name="current_percent" readonly="1"/>
                            <field name="requested_percent"/>
                        </group>
                    </group>
                    <group string="Justificación">
                        <field name="justification" nolabel="1" colspan="2"
                               readonly="state not in ['draft']"/>
                    </group>
                    <group string="Rechazo" invisible="state != 'rejected'">
                        <field name="reject_reason" nolabel="1" colspan="2" readonly="1"/>
                    </group>
                </sheet>
                <chatter/>
            </form>
        </field>
    </record>

    <record id="view_commission_authorization_list" model="ir.ui.view">
        <field name="name">commission.authorization.list</field>
        <field name="model">commission.authorization</field>
        <field name="arch" type="xml">
            <list string="Autorizaciones"
                  decoration-success="state == 'approved'"
                  decoration-danger="state == 'rejected'"
                  decoration-warning="state == 'pending'">
                <field name="name"/>
                <field name="sale_order_id"/>
                <field name="requested_by"/>
                <field name="current_percent" string="% Límite"/>
                <field name="requested_percent" string="% Solicitado"/>
                <field name="state" widget="badge"
                       decoration-success="state == 'approved'"
                       decoration-danger="state == 'rejected'"
                       decoration-warning="state == 'pending'"/>
            </list>
        </field>
    </record>

    <record id="action_commission_authorization" model="ir.actions.act_window">
        <field name="name">Autorizaciones de Comisión</field>
        <field name="res_model">commission.authorization</field>
        <field name="view_mode">list,form</field>
    </record>

    <menuitem id="menu_commission_authorization"
              name="Autorizaciones"
              parent="menu_commission_root"
              action="action_commission_authorization"
              sequence="25"/>
</odoo>```

## ./views/commission_move_views.xml
```xml
<odoo>
    <record id="view_commission_move_list" model="ir.ui.view">
        <field name="name">commission.move.list</field>
        <field name="model">commission.move</field>
        <field name="arch" type="xml">
            <list string="Movimientos" decoration-danger="amount &lt; 0" create="0">
                <field name="date"/>
                <field name="name"/>
                <field name="partner_id"/>
                <field name="sale_order_id"/>
                <field name="amount" sum="Total"/>
                <field name="currency_id" column_invisible="1"/>
                <field name="state" widget="badge" decoration-success="state == 'invoiced'"/>
            </list>
        </field>
    </record>

    <record id="action_commission_move" model="ir.actions.act_window">
        <field name="name">Análisis de Movimientos</field>
        <field name="res_model">commission.move</field>
        <field name="view_mode">list,form,pivot</field>
    </record>
</odoo>
```

## ./views/commission_settlement_views.xml
```xml
<odoo>
    <record id="view_commission_settlement_form" model="ir.ui.view">
        <field name="name">commission.settlement.form</field>
        <field name="model">commission.settlement</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_approve" string="Aprobar" type="object"
                            invisible="state != 'draft'" class="btn-primary"/>
                    <button name="action_create_bill" string="Generar Factura Proveedor" type="object"
                            invisible="state != 'approved'" class="btn-primary"/>
                    <field name="state" widget="statusbar"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1><field name="name"/></h1>
                    </div>
                    <group>
                        <group>
                            <field name="partner_id" readonly="state != 'draft'"/>
                            <field name="date" readonly="state != 'draft'"/>
                        </group>
                        <group>
                            <field name="total_amount"/>
                            <field name="vendor_bill_id" readonly="1"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Detalle de Movimientos">
                            <field name="move_ids" readonly="1">
                                <list>
                                    <field name="sale_order_id"/>
                                    <field name="date"/>
                                    <field name="amount"/>
                                </list>
                            </field>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <record id="action_commission_settlement" model="ir.actions.act_window">
        <field name="name">Liquidaciones</field>
        <field name="res_model">commission.settlement</field>
        <field name="view_mode">list,form</field>
    </record>

    <!-- Menú raíz -->
    <menuitem id="menu_commission_root" name="Comisiones" parent="sale.sale_menu_root" sequence="40"/>

    <!-- Submenús operativos (solo gestores) -->
    <menuitem id="menu_commission_analysis" name="Movimientos Individuales"
              parent="menu_commission_root" action="action_commission_move"
              sequence="10" groups="om_advanced_commission.group_commission_manager"/>
    <menuitem id="menu_commission_settlement" name="Liquidaciones"
              parent="menu_commission_root" action="action_commission_settlement"
              sequence="20" groups="om_advanced_commission.group_commission_manager"/>
</odoo>```

## ./views/res_config_settings_views.xml
```xml
<odoo>
    <record id="res_config_settings_view_form_commission" model="ir.ui.view">
        <field name="name">res.config.settings.view.form.commission</field>
        <field name="model">res.config.settings</field>
        <field name="inherit_id" ref="sale.res_config_settings_view_form"/>
        <field name="arch" type="xml">
            <xpath expr="//block[@name='catalog_setting_container']" position="after">
                <h2>Configuración Comisiones</h2>
                <div class="row mt16 o_settings_container">
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_right_pane">
                            <label for="commission_product_id"/>
                            <field name="commission_product_id"/>
                            <div class="text-muted">
                                Producto servicio usado en facturas de proveedor.
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_right_pane">
                            <label for="commission_journal_id"/>
                            <field name="commission_journal_id"/>
                        </div>
                    </div>
                </div>
            </xpath>
        </field>
    </record>
</odoo>
```

## ./views/sale_order_views.xml
```xml
<odoo>
    <record id="view_order_form_commission" model="ir.ui.view">
        <field name="name">sale.order.form.commission</field>
        <field name="model">sale.order</field>
        <field name="inherit_id" ref="sale.view_order_form"/>
        <field name="arch" type="xml">

            <xpath expr="//header" position="inside">
                <button name="action_recalc_commissions"
                        string="Recalcular Comisiones"
                        type="object"
                        class="btn-secondary"
                        groups="om_advanced_commission.group_commission_manager"
                        invisible="state not in ['sale', 'done']"/>
                <button name="action_request_commission_auth"
                        string="Solicitar Autorización"
                        type="object"
                        class="btn-warning"
                        invisible="not commission_requires_auth"/>
            </xpath>

            <xpath expr="//notebook" position="inside">
                <page string="Gestión de Comisiones" name="commissions_rules">

                    <group string="Vendedores (máx. 2.5% total sin autorización)">
                        <group>
                            <field name="seller1_id"/>
                            <field name="seller1_percent" string="% Vendedor 1"
                                   invisible="not seller1_id"/>
                            <field name="seller2_id"/>
                            <field name="seller2_percent" string="% Vendedor 2"
                                   invisible="not seller2_id"/>
                            <field name="seller3_id"/>
                            <field name="seller3_percent" string="% Vendedor 3"
                                   invisible="not seller3_id"/>
                        </group>
                        <group>
                            <field name="total_seller_percent" readonly="1"/>
                            <field name="total_commission_percent" readonly="1" string="% Total Comisionado"/>
                            <field name="commission_requires_auth" readonly="1"/>
                            <field name="commission_authorization_id" readonly="1"
                                   invisible="not commission_authorization_id"/>
                        </group>
                    </group>

                    <separator string="Otras Comisiones (sin restricción de porcentaje)"/>
                    <field name="commission_rule_ids" domain="[('role_type', '!=', 'internal')]">
                        <list string="Reglas de Comisión" editable="bottom">
                            <field name="role_type"/>
                            <field name="partner_id"/>
                            <field name="calculation_base"/>
                            <field name="percent" invisible="calculation_base == 'manual'"/>
                            <field name="fixed_amount" invisible="calculation_base != 'manual'"/>
                            <field name="estimated_amount" readonly="1" sum="Total Estimado"/>
                            <field name="currency_id" column_invisible="1"/>
                        </list>
                    </field>
                </page>
            </xpath>

            <xpath expr="//field[@name='order_line']/list//field[@name='price_unit']" position="after">
                <field name="no_commission" optional="hide"/>
            </xpath>
        </field>
    </record>
</odoo>```

## ./wizard/__init__.py
```py
from . import commission_make_invoice
from . import commission_report_wizard
from . import commission_authorization_reject_wizard```

## ./wizard/commission_authorization_reject_wizard_views.xml
```xml
<odoo>
    <record id="view_commission_auth_reject_wizard" model="ir.ui.view">
        <field name="name">commission.authorization.reject.wizard.form</field>
        <field name="model">commission.authorization.reject.wizard</field>
        <field name="arch" type="xml">
            <form string="Rechazar Autorización">
                <group>
                    <field name="authorization_id" readonly="1"/>
                    <field name="reject_reason" placeholder="Indica el motivo del rechazo..."/>
                </group>
                <footer>
                    <button name="action_confirm_reject" string="Confirmar Rechazo"
                            type="object" class="btn-danger"/>
                    <button string="Cancelar" special="cancel" class="btn-secondary"/>
                </footer>
            </form>
        </field>
    </record>
</odoo>```

## ./wizard/commission_authorization_reject_wizard.py
```py
from odoo import models, fields


class CommissionAuthorizationRejectWizard(models.TransientModel):
    _name = 'commission.authorization.reject.wizard'
    _description = 'Wizard Rechazo Autorización'

    authorization_id = fields.Many2one('commission.authorization', required=True)
    reject_reason = fields.Text(string='Motivo de Rechazo', required=True)

    def action_confirm_reject(self):
        self.authorization_id.write({
            'state': 'rejected',
            'reject_reason': self.reject_reason,
        })
        self.authorization_id.message_post(
            body=f"❌ Rechazado por {self.env.user.name}: {self.reject_reason}"
        )```

## ./wizard/commission_make_invoice_views.xml
```xml
<odoo>
    <record id="view_commission_make_invoice" model="ir.ui.view">
        <field name="name">commission.make.invoice.form</field>
        <field name="model">commission.make.invoice</field>
        <field name="arch" type="xml">
            <form>
                <group>
                    <field name="date_to"/>
                    <field name="partner_ids" widget="many2many_tags" placeholder="Dejar vacío para todos"/>
                </group>
                <footer>
                    <button name="action_generate_settlements" string="Generar Liquidaciones" type="object" class="btn-primary"/>
                    <button string="Cancelar" special="cancel" class="btn-secondary"/>
                </footer>
            </form>
        </field>
    </record>

    <record id="action_commission_wizard" model="ir.actions.act_window">
        <field name="name">Generar Liquidación Masiva</field>
        <field name="res_model">commission.make.invoice</field>
        <field name="target">new</field>
        <field name="view_mode">form</field>
    </record>

    <menuitem id="menu_commission_wizard" name="Generar Liquidación Masiva" parent="menu_commission_root" action="action_commission_wizard" sequence="5"/>
</odoo>
```

## ./wizard/commission_make_invoice.py
```py
from odoo import models, fields, api


class CommissionMakeInvoice(models.TransientModel):
    _name = 'commission.make.invoice'
    _description = 'Asistente para Generar Liquidación'

    date_to = fields.Date(string='Hasta fecha', default=fields.Date.context_today)
    partner_ids = fields.Many2many('res.partner', string='Comisionistas')

    def action_generate_settlements(self):
        Move = self.env['commission.move']
        Settlement = self.env['commission.settlement']

        domain = [('state', '=', 'draft'), ('date', '<=', self.date_to)]
        if self.partner_ids:
            domain.append(('partner_id', 'in', self.partner_ids.ids))

        moves = Move.search(domain)

        # Agrupar por (partner_id, currency_id, company_id) usando IDs
        grouped = {}
        for m in moves:
            key = (m.partner_id.id, m.currency_id.id, m.company_id.id)
            if key not in grouped:
                grouped[key] = self.env['commission.move']
            grouped[key] |= m

        created_settlements = Settlement
        for (partner_id, currency_id, company_id), partner_moves in grouped.items():
            partner = self.env['res.partner'].browse(partner_id)
            settlement = Settlement.create({
                'partner_id': partner_id,
                'currency_id': currency_id,
                'company_id': company_id,
                'name': f"LIQ-{fields.Date.today()}-{partner.name}",
                'state': 'draft',
                'move_ids': [(6, 0, partner_moves.ids)],
            })
            partner_moves.write({'state': 'settled'})
            created_settlements |= settlement

        return {
            'type': 'ir.actions.act_window',
            'name': 'Liquidaciones Generadas',
            'res_model': 'commission.settlement',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_settlements.ids)],
        }```

## ./wizard/commission_report_wizard_views.xml
```xml
<odoo>
    <record id="view_commission_report_wizard_form" model="ir.ui.view">
        <field name="name">commission.report.wizard.form</field>
        <field name="model">commission.report.wizard</field>
        <field name="arch" type="xml">
            <form string="Reporte de Comisiones">
                <group>
                    <group>
                        <field name="date_from"/>
                        <field name="date_to"/>
                    </group>
                    <group>
                        <field name="allow_previous_months" invisible="1"/>
                        <field name="partner_ids" widget="many2many_tags"
                               placeholder="Todos los vendedores..."
                               invisible="not allow_previous_months"
                               groups="om_advanced_commission.group_commission_authorizer"/>
                    </group>
                </group>
                <div class="alert alert-info" role="alert"
                     invisible="allow_previous_months">
                    Solo puedes consultar comisiones del mes en curso.
                </div>
                <footer>
                    <button name="action_print_report" string="Imprimir PDF" type="object"
                            class="btn-primary" icon="fa-print"/>
                    <button string="Cancelar" special="cancel" class="btn-secondary"/>
                </footer>
            </form>
        </field>
    </record>

    <record id="action_commission_report_wizard" model="ir.actions.act_window">
        <field name="name">Reporte PDF de Comisiones</field>
        <field name="res_model">commission.report.wizard</field>
        <field name="view_mode">form</field>
        <field name="target">new</field>
    </record>

    <!-- Mis Comisiones: accesible para todos los vendedores -->
    <menuitem id="menu_commission_report_sales"
              name="Mis Comisiones"
              parent="menu_commission_root"
              action="action_commission_report_wizard"
              sequence="1"/>

    <!-- Reporte PDF: accesible para gestores -->
    <menuitem id="menu_commission_report"
              name="Reporte PDF"
              parent="menu_commission_root"
              action="action_commission_report_wizard"
              sequence="30"
              groups="om_advanced_commission.group_commission_manager"/>
</odoo>```

## ./wizard/commission_report_wizard.py
```py
from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import date


class CommissionReportWizard(models.TransientModel):
    _name = 'commission.report.wizard'
    _description = 'Asistente de Reporte de Comisiones'

    date_from = fields.Date(string='Desde', required=True)
    date_to = fields.Date(string='Hasta', required=True)
    partner_ids = fields.Many2many('res.partner', string='Vendedores',
                                   help="Dejar vacío para imprimir todos")
    allow_previous_months = fields.Boolean(string='Ver meses anteriores', default=False)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        today = date.today()
        res['date_from'] = today.replace(day=1)
        res['date_to'] = today
        is_auth = self.env.user.has_group('om_advanced_commission.group_commission_authorizer')
        res['allow_previous_months'] = is_auth
        if not is_auth:
            partner = self.env.user.partner_id
            res['partner_ids'] = [(6, 0, [partner.id])]
        return res

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        is_auth = self.env.user.has_group('om_advanced_commission.group_commission_authorizer')
        for rec in self:
            today = date.today()
            current_month_start = today.replace(day=1)
            if not is_auth:
                if rec.date_from and rec.date_from < current_month_start:
                    raise UserError(
                        "Solo puedes consultar comisiones del mes en curso. "
                        "Para ver meses anteriores, solicita acceso a un autorizador."
                    )
                if rec.date_to and rec.date_to > today:
                    raise UserError("La fecha 'Hasta' no puede ser mayor a hoy.")

    def action_print_report(self):
        if not self.env.user.has_group('om_advanced_commission.group_commission_authorizer'):
            partner = self.env.user.partner_id
            if self.partner_ids and partner not in self.partner_ids:
                raise UserError("Solo puedes ver tus propias comisiones.")
            if not self.partner_ids:
                self.partner_ids = [(6, 0, [partner.id])]

        data = {
            'date_from': self.date_from,
            'date_to': self.date_to,
            'partner_ids': self.partner_ids.ids,
        }
        return self.env.ref('om_advanced_commission.action_report_commission_pdf').report_action(self, data=data)```

