# -*- coding: utf-8 -*-
"""19.0.6.0.1 — Depuración de comisiones PENDIENTES sin cobro que las
respalde (hallazgo en QA al validar 6.0.0):

* 32 originales cuya conciliación ya no existía (cobros desconciliados y
  vueltos a conciliar antes de que existiera la reversa automática: la
  segunda conciliación generó su propia comisión y la vieja quedó huérfana);
* 26 originales nacidas de asientos de DIFERENCIA CAMBIARIA (no es dinero;
  el motor ya los ignora desde 3.0.0 pero las comisiones viejas seguían
  pendientes).
Se cancelan con nota en la orden. Lo ya pagado no se toca (queda en el log).
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cancelled = env['commission.move']._commission_purge_unbacked()
    _logger.info('[om_advanced_commission 6.0.1] %d comisión(es) pendientes sin cobro canceladas (%.2f)',
                 len(cancelled), sum(cancelled.mapped('amount')))
