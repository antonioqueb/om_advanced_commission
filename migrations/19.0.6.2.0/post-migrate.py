"""Cierra las actividades "Autorizar comisión" que quedaron colgadas.

Aprobar borraba las actividades de todos los autorizadores, pero rechazar
(wizard) y regresar a borrador no cerraban ninguna. Desde esta versión el
cierre vive en write(); aquí se archivan las de solicitudes ya resueltas.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

FEEDBACK = 'Cerrada por limpieza: el documento ya estaba resuelto.'


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Activity = env['mail.activity'].sudo()
    acts = Activity.search([('res_model', '=', 'commission.authorization'),
                            ('summary', '=', 'Autorizar comisión')])
    auths = env['commission.authorization'].browse(list(set(acts.mapped('res_id')))).exists()
    resolved = {a.id for a in auths if a.state != 'pending'}
    stale = acts.filtered(lambda a: a.res_id in resolved or a.res_id not in auths.ids)
    if stale:
        stale.write({'active': False, 'feedback': FEEDBACK})
    _logger.info('[om_advanced_commission] Autorizar comisión: %s actividad(es) colgadas cerradas.', len(stale))
