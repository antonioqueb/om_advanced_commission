# -*- coding: utf-8 -*-
def migrate(cr, version):
    """Movimientos anteriores al snapshot de regla (rule_role NULL): se les
    asigna el rol de la regla vigente de su orden para ese beneficiario, y
    la propia regla como rule_id. Así el panel los clasifica como vendedor /
    embajador / constructora / referidor en vez de 'Otros'."""
    cr.execute("""
        UPDATE commission_move cm
           SET rule_role = r.role_type,
               rule_id = COALESCE(cm.rule_id, r.id),
               rule_base = COALESCE(cm.rule_base, r.calculation_base),
               rule_percent = CASE WHEN cm.rule_percent IS NULL OR cm.rule_percent = 0
                                   THEN COALESCE(r.percent, 0) ELSE cm.rule_percent END
          FROM sale_commission_rule r
         WHERE cm.rule_role IS NULL
           AND r.sale_order_id = cm.sale_order_id
           AND r.partner_id = cm.partner_id
    """)
