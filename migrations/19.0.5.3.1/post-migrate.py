# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Usuario 66 (Karen) se suma a la excepción de 65 y 75: venden pero
    nunca comisionan. Además, lo PENDIENTE (no pagado) de los excluidos se
    cancela para que no entre a una liquidación; lo ya liquidado/pagado no
    se toca."""
    cr.execute("""
        UPDATE res_partner
           SET commission_excluded = TRUE
         WHERE id IN (SELECT partner_id FROM res_users WHERE id IN (65, 66, 75))
    """)
    env = api.Environment(cr, SUPERUSER_ID, {})
    Move = env['commission.move']
    pending = Move.search([('partner_id.commission_excluded', '=', True), ('state', '=', 'draft')])
    if pending:
        pending.with_context(petty_cash_internal=True).write({'state': 'cancel'})
        for m in pending:
            m.message_post(body='Cancelado por migración 19.0.5.3.1: beneficiario excluido de comisiones.',
                           message_type='notification')
