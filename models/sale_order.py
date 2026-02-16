from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')
    x_project_id = fields.Many2one('project.project', string='Proyecto (Job Name)')

    def action_recalc_commissions(self):
        """
        Recálculo forzado: borra drafts existentes y recrea desde partial reconciles.
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        company_currency = self.company_id.currency_id

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión.", "danger")

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted' and x.payment_state != 'not_paid')
        if not invoices:
            return self._return_notification("Sin facturas pagadas.", "warning")

        # Borrar drafts existentes de esta SO para recalcular limpio
        old_drafts = CommissionMove.search([
            ('sale_order_id', '=', self.id),
            ('state', '=', 'draft'),
        ])
        if old_drafts:
            old_drafts.unlink()

        # Recolectar todos los partial reconciles de las facturas
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
            'params': {
                'title': 'Gestión de Comisiones',
                'message': message,
                'type': type,
                'sticky': False,
            }
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')