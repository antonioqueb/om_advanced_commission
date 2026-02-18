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
        self.write({'state': 'draft'})