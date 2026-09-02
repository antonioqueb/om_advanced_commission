# -*- coding: utf-8 -*-
"""19.0.6.1.0 — Todo cobro de cliente aplicado a factura.

Regla del cliente: un pago sin aplicar es error de captura. Se aplican de
una vez los cobros con saldo sin aplicar a las facturas abiertas de cada
cliente (misma compañía y moneda; primero la factura del memo, luego las
más antiguas). Lo que no encuentre factura queda como anticipo y se aplica
solo cuando se publique la siguiente factura del cliente. Cada aplicación
genera su comisión con la fecha real del cobro.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Payment = env['account.payment'].sudo()
    companies = env['res.company'].sudo().search([])
    total = 0
    for company in companies:
        applied = Payment.with_company(company)._som_auto_apply_all([company.id])
        total += len(applied)
        for pay, inv, amount in applied:
            _logger.info('[om_advanced_commission 6.1.0] %s: %s -> %s %.2f', company.name, pay.name, inv.name, amount)
    remaining = Payment.search(Payment._som_unapplied_payments_domain(companies.ids)).filtered(
        lambda p: p.move_id and p.move_id.state == 'posted' and not p.currency_id.is_zero(p._som_unapplied_amount()))
    _logger.info('[om_advanced_commission 6.1.0] %d aplicación(es) de cobros; %d pago(s) siguen como anticipo '
                 'sin factura abierta (%.2f)', total, len(remaining),
                 sum(p._som_unapplied_amount() for p in remaining))
