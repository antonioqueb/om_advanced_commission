# -*- coding: utf-8 -*-
"""19.0.6.0.0 — Comisiones por FECHA DE COBRO, inicio no retroactivo y
bienes vs servicios por rol.

1. `payment_date` pasa a ser la fecha real del dinero (pago o línea
   bancaria; en reversas/ajustes, la fecha del ajuste): se recalcula para
   todos los movimientos. Antes muchos traían la fecha de la factura o de la
   conciliación (pagos de 2025 aparecían como "agosto 2026").
2. Todo lo PENDIENTE con cobro anterior al inicio de comisiones (Ajustes,
   1 ago 2026) queda como Pagado · cobrada fuera del sistema. Sin asientos,
   sin liquidación: el cliente ya las pagó.
3. Lo cobrado desde el inicio se realinea con la fórmula vigente
   (externos solo sobre bienes; vendedores sobre su base menos externos) y
   se llenan los campos de transparencia (cobrado con IVA, base de cálculo,
   externos descontados). Lo pendiente se corrige en sitio; lo ya pagado
   recibe un ajuste por diferencia (jamás se toca lo pagado).
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Move = env['commission.move'].sudo()

    # 1. Fecha de cobro real para todos.
    moves = Move.with_context(active_test=False).search([])
    env.add_to_compute(Move._fields['payment_date'], moves)
    Move.flush_model(['payment_date'])
    _logger.info('[om_advanced_commission 6.0.0] fecha de cobro recalculada en %d movimiento(s)', len(moves))

    # 2. Corte no retroactivo.
    start = Move._commission_start_date()
    closed = Move._commission_apply_start_date()
    total = sum(closed.mapped('amount'))
    _logger.info('[om_advanced_commission 6.0.0] %d comisión(es) con cobro anterior al %s cerradas como '
                 'pagadas fuera del sistema (%.2f)', len(closed), start, total)

    # 3. Realineo de lo vigente (desde el inicio) con la fórmula nueva.
    Partial = env['account.partial.reconcile'].sudo()
    live = Move.search([('pre_start', '=', False), ('origin_move_id', '=', False),
                        ('state', '!=', 'cancel'), ('partial_reconcile_id', '!=', False),
                        ('payment_date', '>=', start)])
    partials = Partial.browse(sorted(set(live.mapped('partial_reconcile_id').ids)))
    before = {m.id: m.amount for m in live}
    adj_domain = [('origin_move_id', 'in', live.ids), ('adjustment_kind', '=', 'adjustment')]
    adj_before = Move.search_count(adj_domain)
    partials._create_commission_moves(refresh=True)
    live.invalidate_recordset(['amount'])
    changed = [m for m in live if abs((m.amount or 0.0) - before.get(m.id, 0.0)) > 0.005]
    adj_new = Move.search_count(adj_domain) - adj_before
    _logger.info('[om_advanced_commission 6.0.0] %d conciliación(es) realineadas; %d movimiento(s) pendientes '
                 'cambiaron de monto; %d ajuste(s) nuevos sobre comisiones ya pagadas',
                 len(partials), len(changed), adj_new)
    for m in changed[:50]:
        _logger.info('   %s %s: %.2f -> %.2f', m.name, m.partner_id.display_name, before.get(m.id, 0.0), m.amount)
