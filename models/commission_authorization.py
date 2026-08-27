from odoo import models, fields, api
from odoo.exceptions import UserError


class CommissionAuthorization(models.Model):
    _name = 'commission.authorization'
    _description = 'Solicitud de Autorización de Comisión Extra'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Referencia', readonly=True, default='Nueva Solicitud')
    sale_order_id = fields.Many2one('sale.order', string='Orden de Venta', required=True)
    auth_type = fields.Selection([
        ('seller', 'Vendedores internos'),
        ('external', 'Comisión externa'),
    ], string='Tipo', required=True, default='seller')
    requested_by = fields.Many2one('res.users', string='Solicitado por',
                                   default=lambda self: self.env.user, readonly=True)
    # Odoo 19 renombró res.users.groups_id → group_ids.
    authorizer_id = fields.Many2one(
        'res.users', string='Autorizador',
        domain=lambda self: [
            (
                'group_ids' if 'group_ids' in self.env['res.users']._fields else 'groups_id',
                'in',
                [self.env.ref('om_advanced_commission.group_commission_authorizer').id],
            )
        ],
    )
    requested_percent = fields.Float(string='% Solicitado', required=True)
    current_percent = fields.Float(string='% Actual Permitido', readonly=True)
    approved_percent = fields.Float(
        string='% Aprobado',
        help='El autorizador puede aprobar un porcentaje menor al solicitado. Es el que queda permitido en la orden.')
    justification = fields.Text(string='Justificación')
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('pending', 'Pendiente'),
        ('approved', 'Aprobado'),
        ('rejected', 'Rechazado'),
    ], default='draft', tracking=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    reject_reason = fields.Text(string='Motivo de Rechazo', readonly=True)
    authorization_date = fields.Datetime(string='Fecha de Resolución', readonly=True)
    authorized_by = fields.Many2one('res.users', string='Resuelto por', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'Nueva Solicitud') == 'Nueva Solicitud':
                so = self.env['sale.order'].browse(vals.get('sale_order_id'))
                vals['name'] = f"AUTH-{so.name or 'nuevo'}"
            if not vals.get('current_percent') and vals.get('sale_order_id'):
                so = self.env['sale.order'].browse(vals['sale_order_id'])
                if vals.get('auth_type', 'seller') == 'seller':
                    vals['current_percent'] = so.commission_allowed_seller_percent
                else:
                    vals['current_percent'] = max(so._commission_external_max(),
                                                  so.commission_allowed_external_percent or 0.0)
        return super().create(vals_list)

    @api.onchange('sale_order_id', 'auth_type')
    def _onchange_order_type(self):
        so = self.sale_order_id
        if not so:
            return
        if self.auth_type == 'seller':
            self.requested_percent = so.total_seller_percent
            self.current_percent = so.commission_allowed_seller_percent
        else:
            self.requested_percent = so.total_external_percent
            self.current_percent = max(so._commission_external_max(), so.commission_allowed_external_percent or 0.0)

    def _is_authorizer(self):
        return self.env.user.has_group('om_advanced_commission.group_commission_authorizer')

    def action_submit(self):
        for rec in self:
            if rec.requested_percent <= rec.current_percent:
                raise UserError("El % solicitado no supera el permitido actual; no requiere autorización.")
            rec.write({'state': 'pending'})
            note = ("Solicitud de autorización de comisión (%s) para %s: %s%% (permitido %s%%)"
                    % (dict(rec._fields['auth_type'].selection)[rec.auth_type],
                       rec.sale_order_id.name, rec.requested_percent, rec.current_percent))
            targets = rec.authorizer_id or self.env['commission.move']._commission_authorizer_users()
            for user in targets:
                rec.activity_schedule('mail.mail_activity_data_todo', user_id=user.id,
                                      summary='Autorizar comisión', note=note)

    def _close_activities(self):
        self.activity_ids.filtered(lambda a: a.summary == 'Autorizar comisión').unlink()

    def action_approve(self):
        if not self._is_authorizer():
            raise UserError("No tienes permisos para autorizar comisiones.")
        for rec in self:
            if rec.state != 'pending':
                raise UserError("Solo se aprueban solicitudes pendientes.")
            approved = rec.approved_percent or rec.requested_percent
            if approved <= 0:
                raise UserError("Captura un % aprobado mayor a cero.")
            rec.write({
                'state': 'approved',
                'approved_percent': approved,
                'authorization_date': fields.Datetime.now(),
                'authorized_by': self.env.user.id,
            })
            so = rec.sale_order_id.sudo()
            field = ('commission_allowed_seller_percent' if rec.auth_type == 'seller'
                     else 'commission_allowed_external_percent')
            so.with_context(commission_sync=True).write({
                field: max(approved, so[field] or 0.0),
                'commission_authorization_id': rec.id,
            })
            # Tiempo real: el excedente retenido se libera en este instante.
            so._commission_refresh()
            rec._close_activities()
            body = "✅ Autorización aprobada por %s: %s%% permitido (%s)" % (
                self.env.user.name, approved, dict(rec._fields['auth_type'].selection)[rec.auth_type])
            rec.message_post(body=body)
            so.message_post(body=body, message_type='notification')
            if rec.requested_by:
                so.activity_schedule('mail.mail_activity_data_todo', user_id=rec.requested_by.id,
                                     summary='Comisión autorizada', note=body)

    def action_reject(self):
        if not self._is_authorizer():
            raise UserError("No tienes permisos para rechazar autorizaciones.")
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'commission.authorization.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_authorization_id': self.id},
        }

    def action_reset_draft(self):
        self.write({'state': 'draft', 'reject_reason': False})
