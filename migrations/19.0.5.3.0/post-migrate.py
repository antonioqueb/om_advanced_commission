# -*- coding: utf-8 -*-
def migrate(cr, version):
    """Excepción acordada con el cliente (27 ago 2026): los usuarios 75 y 65
    venden pero NUNCA comisionan. Solo hacia adelante: no se tocan
    movimientos ya devengados. Editable después en el contacto/usuario."""
    cr.execute("""
        UPDATE res_partner
           SET commission_excluded = TRUE
         WHERE id IN (SELECT partner_id FROM res_users WHERE id IN (65, 75))
    """)
