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
        )