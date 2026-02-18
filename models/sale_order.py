from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
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
            needs = so.total_seller_percent > SELLER_MAX_PCT
            auth_ok = (
                so.commission_authorization_id and
                so.commission_authorization_id.state == 'approved'
            )
            so.commission_requires_auth = needs and not auth_ok

    @api.onchange('seller1_id', 'seller2_id', 'seller3_id',
                  'seller1_percent', 'seller2_percent', 'seller3_percent')
    def _onchange_sellers(self):
        self._sync_seller_rules()
        total = (self.seller1_percent or 0) + (self.seller2_percent or 0) + (self.seller3_percent or 0)
        if total > SELLER_MAX_PCT:
            auth = self.commission_authorization_id
            if not auth or auth.state != 'approved':
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
        # Validar autorización AQUÍ, no al guardar
        if self.total_seller_percent > SELLER_MAX_PCT:
            auth_ok = (
                self.commission_authorization_id and
                self.commission_authorization_id.state == 'approved'
            )
            if not auth_ok:
                raise UserError(
                    f"El porcentaje total de vendedores ({self.total_seller_percent}%) supera el límite de "
                    f"{SELLER_MAX_PCT}%. Obtén una autorización aprobada antes de recalcular."
                )

        CommissionMove = self.env['commission.move']

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted' and x.payment_state != 'not_paid')
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
    no_commission = fields.Boolean(string='Excluir de Comisión')