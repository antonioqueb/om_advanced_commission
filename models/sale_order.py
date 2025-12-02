from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')

    def action_recalc_commissions(self):
        """
        Botón de emergencia/manual para generar comisiones.
        Incluye extracción segura de datos de conciliación para Odoo 19.
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        logs = []

        # 1. Buscar facturas asociadas (Publicadas)
        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            raise UserError("No hay facturas publicadas/validadas asociadas a este pedido.")

        if not self.commission_rule_ids:
            raise UserError("No has definido Reglas de Comisión en la pestaña 'Gestión de Comisiones'.")

        # 2. Recorrer facturas y sus pagos
        for inv in invoices:
            if inv.payment_state == 'not_paid':
                logs.append(f"Factura {inv.name}: No tiene pagos.")
                continue

            # Obtener datos de conciliación
            reconciled_partials = inv._get_reconciled_invoices_partials()
            
            if not reconciled_partials:
                logs.append(f"Factura {inv.name}: Estado {inv.payment_state} pero no detecto conciliaciones.")
                continue

            # --- EXTRACCIÓN SEGURA DE DATOS (FIX ODOO 19) ---
            for data in reconciled_partials:
                partial = None
                amount = 0.0
                counterpart_line = None

                # Caso A: Es un Diccionario (Estándar Odoo moderno)
                if isinstance(data, dict):
                    partial = data.get('partial_id')
                    amount = data.get('amount', 0.0)
                    counterpart_line = data.get('counterpart_line_id')
                
                # Caso B: Es una Tupla/Lista con datos (Legado)
                elif isinstance(data, (list, tuple)) and len(data) >= 3:
                    partial = data[0]
                    amount = data[1]
                    counterpart_line = data[2]
                
                # Caso C: Dato vacío o desconocido -> Ignorar para evitar Crash
                else:
                    _logger.warning(f"[COMMISSION] Formato de conciliación desconocido o vacío en {inv.name}: {data}")
                    continue

                # Validación final de integridad
                if not counterpart_line:
                    continue

                # --- FIN EXTRACCIÓN ---

                # counterpart_line es la linea del asiento contable del pago
                payment_move = counterpart_line.move_id
                payment_obj = self.env['account.payment'].search([('move_id', '=', payment_move.id)], limit=1)
                
                # Si es nota de crédito o reversión
                is_refund = (inv.move_type == 'out_refund')

                # Calcular ratio
                if inv.amount_total == 0:
                    continue
                
                ratio = amount / inv.amount_total
                sign = -1 if is_refund else 1

                # 3. Generar Comisiones
                for rule in self.commission_rule_ids:
                    # Verificar duplicados
                    existing = CommissionMove.search([
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('payment_id', '=', payment_obj.id if payment_obj else False),
                        ('is_refund', '=', is_refund),
                        ('amount', '=', (rule.estimated_amount * ratio * sign)) 
                    ])
                    
                    if existing:
                        logs.append(f"Omitido: Ya existe comisión para {rule.partner_id.name} sobre {payment_move.name}.")
                        continue

                    # Cálculo del monto
                    comm_amount = 0.0
                    if rule.calculation_base == 'manual':
                        comm_amount = rule.fixed_amount * ratio
                    else:
                        comm_amount = rule.estimated_amount * ratio
                    
                    comm_amount *= sign

                    if comm_amount != 0:
                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': self.id,
                            'payment_id': payment_obj.id if payment_obj else False,
                            'invoice_line_id': inv.invoice_line_ids[0].id if inv.invoice_line_ids else False,
                            'amount': comm_amount,
                            'base_amount_paid': amount * sign,
                            'currency_id': self.currency_id.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Manual: {inv.name} ({round(ratio*100, 1)}%)"
                        })
                        created_count += 1
                        logs.append(f"CREADO: {comm_amount} para {rule.partner_id.name}")

        # Resultado
        message = f"Proceso finalizado.\nComisiones generadas: {created_count}\n\nDetalle:\n" + "\n".join(logs)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Cálculo de Comisiones',
                'message': message,
                'type': 'success' if created_count > 0 else 'warning',
                'sticky': False, # Cambiado a False para que no moleste si hay muchos logs
            }
        }

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')