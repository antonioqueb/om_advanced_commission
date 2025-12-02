from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

# Instancia del logger para ver los mensajes en Docker
_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    commission_rule_ids = fields.One2many('sale.commission.rule', 'sale_order_id', string='Reglas de Comisión')

    def action_recalc_commissions(self):
        """
        Botón de emergencia/manual para generar comisiones.
        Versión: Acceso directo a líneas contables con LOGS DE DEPURACIÓN.
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        debug_logs = []

        _logger.info(f"[COMMISSION-DEBUG] Iniciando recálculo para Orden: {self.name}")

        # 1. Buscar facturas validadas
        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            msg = "Sin facturas publicadas (posted) asociadas."
            _logger.warning(f"[COMMISSION-DEBUG] {msg}")
            return self._return_notification(msg, "warning")

        if not self.commission_rule_ids:
            msg = "Faltan definir las Reglas de Comisión en el pedido."
            _logger.warning(f"[COMMISSION-DEBUG] {msg}")
            return self._return_notification(msg, "danger")

        # 2. Recorrer facturas
        for inv in invoices:
            _logger.info(f"[COMMISSION-DEBUG] Analizando Factura: {inv.name} | Estado Pago: {inv.payment_state}")

            if inv.payment_state == 'not_paid':
                _logger.info(f"[COMMISSION-DEBUG] SALTADO: La factura {inv.name} no tiene pagos registrados.")
                continue

            # --- ESTRATEGIA: LEER LÍNEAS CONTABLES DIRECTAMENTE ---
            # Filtramos líneas de tipo "Cobrar" (asset_receivable)
            receivable_lines = inv.line_ids.filtered(lambda l: l.account_type == 'asset_receivable')
            
            if not receivable_lines:
                _logger.warning(f"[COMMISSION-DEBUG] ALERTA: La factura {inv.name} no tiene líneas de tipo 'asset_receivable'. Revisa la configuración de la cuenta contable.")
            
            # Recolectamos conciliaciones parciales desde estas líneas
            partials = self.env['account.partial.reconcile']
            for line in receivable_lines:
                # matched_credit_ids: Pagos aplicados a esta factura
                partials |= line.matched_credit_ids
                # matched_debit_ids: Si fuera nota de crédito
                partials |= line.matched_debit_ids

            if not partials:
                msg = f"Factura {inv.name}: Tiene estado pagado/parcial, pero no se encontraron conciliaciones (partials) en las líneas contables."
                _logger.info(f"[COMMISSION-DEBUG] {msg}")
                debug_logs.append(msg)
                continue

            # Procesar cada conciliación
            for partial in partials:
                amount = partial.amount
                _logger.info(f"[COMMISSION-DEBUG] Conciliación encontrada. Monto: {amount}")
                
                # Identificar contrapartida (El Pago)
                if partial.debit_move_id in inv.line_ids:
                    counterpart_line = partial.credit_move_id
                else:
                    counterpart_line = partial.debit_move_id

                payment_move = counterpart_line.move_id
                # Buscamos el objeto pago (si existe)
                payment_obj = self.env['account.payment'].search([('move_id', '=', payment_move.id)], limit=1)

                is_refund = (inv.move_type == 'out_refund')
                
                if inv.amount_total == 0:
                    _logger.info(f"[COMMISSION-DEBUG] Factura monto 0, saltando.")
                    continue
                
                ratio = amount / inv.amount_total
                sign = -1 if is_refund else 1
                amount_signed = amount * sign

                # 3. Generar Comisiones
                for rule in self.commission_rule_ids:
                    # Candado Anti-Duplicados: 
                    # Usamos un rango pequeño para 'base_amount_paid' para evitar errores de redondeo float
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('base_amount_paid', '>=', amount_signed - 0.02),
                        ('base_amount_paid', '<=', amount_signed + 0.02),
                    ]
                    
                    if payment_obj:
                        domain.append(('payment_id', '=', payment_obj.id))
                    else:
                        domain.append(('payment_id', '=', False))
                        
                    existing = CommissionMove.search(domain)
                    if existing:
                        _logger.info(f"[COMMISSION-DEBUG] IGNORADO: Ya existe comisión para {rule.partner_id.name} sobre este pago.")
                        continue

                    # Calcular Monto
                    comm_amount = 0.0
                    if rule.calculation_base == 'manual':
                        comm_amount = rule.fixed_amount * ratio
                    else:
                        comm_amount = rule.estimated_amount * ratio
                    
                    comm_amount *= sign

                    if abs(comm_amount) > 0.01: # Ignorar centavos residuales
                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': self.id,
                            'payment_id': payment_obj.id if payment_obj else False,
                            'invoice_line_id': inv.invoice_line_ids[0].id if inv.invoice_line_ids else False,
                            'amount': comm_amount,
                            'base_amount_paid': amount_signed,
                            'currency_id': self.currency_id.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {inv.name}"
                        })
                        created_count += 1
                        msg_success = f"Generada: {comm_amount} ({rule.partner_id.name})"
                        _logger.info(f"[COMMISSION-DEBUG] {msg_success}")
                        debug_logs.append(msg_success)

        # Mensaje final al usuario
        final_msg = f"Proceso finalizado. {created_count} comisiones generadas."
        if debug_logs:
             final_msg += "\nDetalles:\n" + "\n".join(debug_logs)

        return self._return_notification(
            final_msg, 
            "success" if created_count > 0 else "info"
        )

    def _return_notification(self, message, type='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Gestión de Comisiones',
                'message': message,
                'type': type,
                'sticky': False,
            }
        }

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    no_commission = fields.Boolean(string='Excluir de Comisión')