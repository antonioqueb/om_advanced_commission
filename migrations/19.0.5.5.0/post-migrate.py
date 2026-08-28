# -*- coding: utf-8 -*-
"""Detecta órdenes donde un usuario interno se puso en 'Otras comisiones'
(brincándose el tope de vendedores). NO borra nada: deja nota en cada orden y
una actividad a los Administradores de Comisiones con la lista, para que
decidan (borrar la regla genera el ajuste/reversa automático)."""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Rule = env['sale.commission.rule']
    bad = Rule.search([('role_type', '!=', 'internal')]).filtered(
        lambda r: Rule._partner_is_internal_user(r.partner_id)
        or (r.sale_order_id and r.partner_id in (
            r.sale_order_id.seller1_id | r.sale_order_id.seller2_id | r.sale_order_id.seller3_id)))
    if not bad:
        _logger.info('[om_advanced_commission] Sin reglas externas con usuarios internos.')
        return
    lines = []
    for r in bad:
        so = r.sale_order_id
        lines.append('%s: %s (%s, %s%%)' % (so.name if so else '-', r.partner_id.display_name,
                                          dict(r._fields['role_type'].selection).get(r.role_type), r.percent))
        if so:
            so.message_post(body=('⚠️ Revisión de comisiones: %s es usuario interno y está en '
                                  "'Otras comisiones' (%s, %s%%). Desde la versión 19.0.5.5.0 esto ya "
                                  'no se permite; un Administrador de Comisiones debe decidir si se elimina.')
                            % (r.partner_id.display_name,
                               dict(r._fields['role_type'].selection).get(r.role_type), r.percent),
                            message_type='notification')
    _logger.warning('[om_advanced_commission] %d regla(s) externa(s) con usuario interno:\n%s',
                    len(bad), '\n'.join(lines))
    Move = env['commission.move']
    note = ('Se detectaron %d comisión(es) externa(s) asignadas a usuarios internos (brinco al tope de '
            'vendedores). Revisa cada orden y elimina la regla si procede:\n%s') % (len(bad), '\n'.join(lines))
    for user in Move._commission_manager_users():
        user.partner_id.sudo().activity_schedule(
            'mail.mail_activity_data_todo', user_id=user.id,
            summary='Comisiones externas con usuarios internos', note=note)
