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
        Versión: Acceso directo a líneas contables (Blindado).
        """
        self.ensure_one()
        CommissionMove = self.env['commission.move']
        created_count = 0
        logs = []

        # 1. Buscar facturas validadas
        invoices = self.invoice_ids.filtered(lambda x: x.state == 'posted')
        
        if not invoices:
            # Si no hay facturas, no hay nada que cobrar.
            return self._return_notification("Sin facturas válidas.", "warning")

        if not self.commission_rule_ids:
            return self._return_notification("Faltan definir las Reglas de Comisión en el pedido.", "danger")

        # 2. Recorrer facturas
        for inv in invoices:
            if inv.payment_state == 'not_paid':
                continue

            # --- ESTRATEGIA: LEER LÍNEAS CONTABLES DIRECTAMENTE ---
            # En lugar de usar métodos 'helpers' que fallan en Odoo 19,
            # vamos a la fuente: Las líneas 'Por Cobrar' de la factura.
            
            # Filtramos líneas de tipo "Cobrar" (asset_receivable)
            receivable_lines = inv.line_ids.filtered(lambda l: l.account_type == 'asset_receivable')
            
            # Recolectamos conciliaciones parciales desde estas líneas
            partials = self.env['account.partial.reconcile']
            for line in receivable_lines:
                # matched_credit_ids: Pagos aplicados a esta factura (si es factura cliente)
                partials |= line.matched_credit_ids
                # matched_debit_ids: Si fuera nota de crédito
                partials |= line.matched_debit_ids

            if not partials:
                # Caso raro: Está pagada pero no tiene conciliación parcial (ej. Asiento manual directo)
                logs.append(f"Factura {inv.name}: Pagada pero sin rastro de conciliación estándar.")
                continue

            # Procesar cada conciliación
            for partial in partials:
                amount = partial.amount
                
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
                    continue
                
                ratio = amount / inv.amount_total
                sign = -1 if is_refund else 1

                # 3. Generar Comisiones
                for rule in self.commission_rule_ids:
                    # Candado Anti-Duplicados: Misma regla, mismo pago, mismo monto base
                    domain = [
                        ('sale_order_id', '=', self.id),
                        ('partner_id', '=', rule.partner_id.id),
                        ('base_amount_paid', '=', amount * sign)
                    ]
                    # Si es un pago real, lo usamos en el filtro. Si es asiento vario, no.
                    if payment_obj:
                        domain.append(('payment_id', '=', payment_obj.id))
                    else:
                        # Si es asiento manual, usamos referencia aproximada
                        # Esto evita que se creen infinitamente si pulsas el botón 10 veces
                        domain.append(('payment_id', '=', False))
                        
                    existing = CommissionMove.search(domain)
                    if existing:
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
                            'base_amount_paid': amount * sign,
                            'currency_id': self.currency_id.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {inv.name}"
                        })
                        created_count += 1
                        logs.append(f"+ {comm_amount} ({rule.partner_id.name})")

        return self._return_notification(
            f"Listo. {created_count} comisiones generadas.", 
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