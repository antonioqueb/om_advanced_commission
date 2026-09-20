"""Limpia las actividades que colgaban del CONTACTO del administrador.

- "Cobros sin comisión": desde 6.3.0 es una actividad libre (sin documento)
  que el cron crea y cierra; las viejas sobre res.partner se archivan y el
  cron nocturno vuelve a avisar si sigue faltando algo.
- "Comisiones externas con usuarios internos": aviso único de la migración
  5.5.0; se archiva.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Activity = env['mail.activity'].sudo()
    old = Activity.search([('res_model', '=', 'res.partner'),
                           ('summary', 'in', ['Cobros sin comisión',
                                              'Comisiones externas con usuarios internos'])])
    if old:
        old.write({'active': False,
                   'feedback': 'Archivada por limpieza: el aviso no pertenece al contacto.'})
    _logger.info('[om_advanced_commission] %s aviso(s) sobre contactos archivados.', len(old))
