from datetime import timedelta

from odoo import models, fields, api


class CommissionAudit(models.TransientModel):
    _name = 'commission.audit'
    _description = 'Auditoría: cobros sin comisión'

    date_from = fields.Date(string='Cobros desde', required=True,
                            default=lambda self: fields.Date.context_today(self) - timedelta(days=90))
    line_ids = fields.One2many('commission.audit.line', 'audit_id', string='Cobros sin comisión')
    line_count = fields.Integer(compute='_compute_line_count')

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new', 'name': 'Auditoría de Cobros',
        }

    def action_scan(self):
        self.ensure_one()
        self.line_ids.unlink()
        Move = self.env['commission.move']
        partials = Move._commission_missing_partials(date_from=self.date_from)
        vals = []
        for p in partials:
            side = p._commission_invoice_side()
            if not side:
                continue
            invoice = side[0]
            orders = invoice._commission_orders()
            vals.append((0, 0, {
                'partial_id': p.id,
                'invoice_id': invoice.id,
                'invoice_name': invoice.name,
                'payment_date': p.max_date,
                'amount': p.amount,
                'order_id': orders[:1].id,
                'customer': invoice.partner_id.display_name,
                'reason': self._diagnose(invoice, orders),
            }))
        self.line_ids = vals
        return self._reopen()

    @api.model
    def _diagnose(self, invoice, orders):
        if not orders:
            return 'Factura sin orden con reglas'
        so = orders[0]
        if so.commission_requires_auth:
            return 'Autorización pendiente (se calcula con retención al generar)'
        rules = so.commission_rule_ids
        if all(r.calculation_base == 'margin' for r in rules):
            return 'Todas las reglas por margen: margen 0 o costo no definido'
        return 'Sin movimiento (error al conciliar o conciliación previa al módulo)'

    def action_generate(self):
        self.ensure_one()
        partials = self.line_ids.mapped('partial_id')
        if partials:
            partials.sudo()._create_commission_moves()
        return self.action_scan()


class CommissionAuditLine(models.TransientModel):
    _name = 'commission.audit.line'
    _description = 'Línea de auditoría de cobros'

    audit_id = fields.Many2one('commission.audit', ondelete='cascade')
    partial_id = fields.Many2one('account.partial.reconcile', string='Conciliación')
    invoice_id = fields.Many2one('account.move', string='Factura')
    invoice_name = fields.Char(string='Factura')
    order_id = fields.Many2one('sale.order', string='Orden')
    customer = fields.Char(string='Cliente')
    payment_date = fields.Date(string='Fecha Cobro')
    amount = fields.Float(string='Monto Conciliado')
    reason = fields.Char(string='Diagnóstico')
