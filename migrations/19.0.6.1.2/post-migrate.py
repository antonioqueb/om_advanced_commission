# -*- coding: utf-8 -*-
"""19.0.6.1.2 — Reversas sobre comisiones anteriores al inicio.

Una comisión con cobro anterior al 1 ago 2026 quedó marcada "pagada fuera
del sistema". Si después alguien desconcilió ese cobro (p. ej. para corregir
la factura y volverlo a aplicar), el módulo generó una REVERSA negativa
pendiente que le descontaría al vendedor dinero que el sistema nunca le
pagó. Desde esta versión ya no se generan; aquí se cancelan las que hayan
quedado. Una devolución real entra por nota de crédito + pago de salida.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Move = env['commission.move'].sudo()
    revs = Move.search([('adjustment_kind', '=', 'reversal'), ('state', 'in', ('draft', 'settled')),
                        ('origin_move_id.pre_start', '=', True)])
    for rev in revs:
        if rev.settlement_id and rev.settlement_id.state == 'invoiced':
            continue
        if rev.settlement_id:
            rev.settlement_id.message_post(
                body='Movimiento %s retirado: reversa de una comisión anterior al inicio de comisiones.' % rev.name)
        rev.write({'state': 'cancel', 'settlement_id': False})
        if rev.sale_order_id:
            rev.sale_order_id.message_post(
                body='Reversa %s (%s, %.2f) cancelada: la comisión original es anterior al inicio de comisiones '
                     'y ya estaba pagada fuera del sistema; no procede descuento.'
                % (rev.name, rev.partner_id.display_name, rev.amount), message_type='notification')
    _logger.info('[om_advanced_commission 6.1.2] %d reversa(s) sobre comisiones pre-inicio canceladas (%.2f)',
                 len(revs), sum(revs.mapped('amount')))
