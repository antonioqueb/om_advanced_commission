# -*- coding: utf-8 -*-
def migrate(cr, version):
    """Gate de autorización por % PERMITIDO sin tocar lo capturado.

    Las órdenes existentes se respetan tal cual: su permitido queda en
    max(2.5, total capturado) para que NINGUNA orden ya definida empiece a
    retener. Solo las órdenes nuevas parten del tope de Ajustes."""
    cr.execute("""
        UPDATE sale_order
           SET commission_allowed_seller_percent = GREATEST(2.5, COALESCE(total_seller_percent, 0))
    """)
    # Campos almacenados: el UPDATE no dispara compute; se alinean a mano
    # (tras el grandfathering ninguna orden existente requiere autorización).
    cr.execute("""
        UPDATE sale_order
           SET commission_requires_auth = FALSE,
               commission_seller_effective_percent = COALESCE(total_seller_percent, 0)
    """)
    # Posición del vendedor en las reglas internas existentes (sync en sitio).
    for slot in (1, 2, 3):
        cr.execute("""
            UPDATE sale_commission_rule r
               SET seller_slot = %s
              FROM sale_order so
             WHERE r.sale_order_id = so.id
               AND r.role_type = 'internal'
               AND COALESCE(r.seller_slot, 0) = 0
               AND r.partner_id = so.seller%s_id
               AND NOT EXISTS (SELECT 1 FROM sale_commission_rule r2
                                WHERE r2.sale_order_id = so.id AND r2.seller_slot = %s)
        """, (slot, slot, slot))
    # Factura del movimiento a partir de la línea (nuevo campo invoice_id).
    cr.execute("""
        UPDATE commission_move cm
           SET invoice_id = aml.move_id
          FROM account_move_line aml
         WHERE aml.id = cm.invoice_line_id
           AND cm.invoice_id IS NULL
    """)
    cr.execute("""
        UPDATE commission_move cm
           SET invoice_name = am.name
          FROM account_move am
         WHERE am.id = cm.invoice_id
           AND cm.invoice_name IS NULL
    """)
