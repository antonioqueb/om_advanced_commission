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
        Versión Directa a DB: Lee account.partial.reconcile para máxima compatibilidad.
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

        # 2. Recorrer facturas
        for inv in invoices:
            if inv.payment_state == 'not_paid':
                logs.append(f"Factura {inv.name}: Pendiente de pago.")
                continue

            # --- ESTRATEGIA DIRECTA DB (Blindada contra cambios de Odoo 19) ---
            # Buscamos todas las líneas de esta factura
            # y vemos si tienen 'matched_debit_ids' o 'matched_credit_ids' (Conciliaciones)
            
            # Recolectamos todos los parciales asociados a las líneas de la factura
            partials = self.env['account.partial.reconcile'].search([
                '|',
                ('debit_move_id', 'in', inv.line_ids.ids),
                ('credit_move_id', 'in', inv.line_ids.ids)
            ])

            if not partials:
                logs.append(f"Factura {inv.name}: Estado {inv.payment_state} pero no se hallaron registros en partial_reconcile.")
                continue

            # Procesar cada conciliación encontrada
            for partial in partials:
                amount = partial.amount
                counterpart_line = None

                # Determinar cuál lado es la factura y cuál el pago
                if partial.debit_move_id.move_id.id == inv.id:
                    # La factura estaba en el Debe, el pago viene del Haber (Credit)
                    counterpart_line = partial.credit_move_id
                elif partial.credit_move_id.move_id.id == inv.id:
                    # La factura estaba en el Haber, el pago viene del Debe (Debit)
                    counterpart_line = partial.debit_move_id
                else:
                    # Raro: El parcial apareció pero no coincide el ID de factura (Movimientos cruzados)
                    continue

                # El movimiento contrapartida (Pago)
                payment_move = counterpart_line.move_id
                
                # Intentar buscar el objeto payment asociado (puede ser None si es un asiento manual)
                payment_obj = self.env['account.payment'].search([('move_id', '=', payment_move.id)], limit=1)

                # --- FIN ESTRATEGIA DIRECTA ---

                # Si es nota de crédito o reversión
                is_refund = (inv.move_type == 'out_refund')

                # Calcular ratio
                if inv.amount_total == 0:
                    continue
                
                ratio = amount / inv.amount_total
                sign = -1 if is_refund else 1

                # 3. Generar Comisiones
                for rule in self.commission_rule_ids:
                    # Verificar duplicados exactos
                    # Buscamos si ya existe una comisión generada por este Pago específico
                    # Si payment_obj no existe (asiento manual), usamos el payment_move.name para rastrear o permitimos si no hay ID.
                    
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('is_refund', '=', is_refund),
                        ('base_amount_paid', '=', amount * sign) # Candado por monto base
                    ]
                    
                    if payment_obj:
                        domain.append(('payment_id', '=', payment_obj.id))
                    
                    existing = CommissionMove.search(domain)
                    
                    if existing:
                        logs.append(f"Omitido: Ya procesado {rule.partner_id.name} | Pago: {amount}")
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
                'sticky': False,
            }
        }

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')