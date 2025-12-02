## ./__init__.py
```py
from . import models
from . import wizard
```

## ./__manifest__.py
```py
{
    'name': 'Gestión Avanzada de Comisiones (Cash Basis & Proyectos)',
    'version': '19.0.1.0.0',
    'category': 'Sales/Commissions',
    'summary': 'Motor de comisiones multi-agente basado en pagos, margenes y liquidaciones.',
    'author': 'Custom AI Solution',
    'depends': ['sale_management', 'account', 'purchase'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'views/res_config_settings_views.xml',
        'views/sale_order_views.xml',
        'views/commission_move_views.xml',
        'views/commission_settlement_views.xml',
        'wizard/commission_make_invoice_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
```

## ./data/sequence_data.xml
```xml
<odoo>
    <record id="seq_commission_move" model="ir.sequence">
        <field name="name">Commission Move</field>
        <field name="code">commission.move</field>
        <field name="prefix">COMM/</field>
        <field name="padding">5</field>
    </record>
</odoo>
```

## ./models/__init__.py
```py
from . import res_config_settings
from . import commission_rule
from . import commission_move
from . import commission_settlement
from . import sale_order
from . import account_move
```

## ./models/account_move.py
```py
from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)

class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        """ 
        Detecta pagos sobre facturas de venta y calcula comisión proporcional.
        Incluye LOGGING para depuración en Odoo 19.
        """
        CommissionMove = self.env['commission.move']
        
        for rec in self:
            try:
                # 1. Identificar factura (debit) y pago (credit)
                invoice = rec.debit_move_id.move_id
                payment = rec.credit_move_id.move_id
                amount_reconciled = rec.amount
                is_refund = False

                # Caso inverso (Devoluciones o configuración contable opuesta)
                if invoice.move_type not in ['out_invoice', 'out_refund']:
                    invoice = rec.credit_move_id.move_id
                    payment = rec.debit_move_id.move_id
                
                # _logger.info(f"[COMMISSION] Procesando conciliación: {amount_reconciled} para Doc: {invoice.name} Tipo: {invoice.move_type}")

                if invoice.move_type == 'out_refund':
                    is_refund = True
                    invoice_origin = invoice.reversed_entry_id or invoice
                else:
                    invoice_origin = invoice

                if invoice_origin.move_type not in ['out_invoice', 'out_refund']:
                    # _logger.info(f"[COMMISSION] Ignorado: El documento {invoice_origin.name} no es factura de cliente.")
                    continue

                # 2. Buscar Ventas relacionadas (La parte crítica)
                # Intentamos obtener las lineas de venta vinculadas a las lineas de factura
                sale_line_ids = invoice_origin.invoice_line_ids.mapped('sale_line_ids')
                sale_orders = sale_line_ids.mapped('order_id')
                
                if not sale_orders:
                    # Intento de rescate: Buscar por el campo 'invoice_ids' en sale.order si existe la relación inversa
                    # Esto ayuda en casos de anticipos complejos
                    sale_orders = self.env['sale.order'].search([('invoice_ids', 'in', invoice_origin.id)])
                    if not sale_orders:
                        _logger.warning(f"[COMMISSION] ALERTA: Factura {invoice_origin.name} pagada, pero no se encontró Orden de Venta vinculada.")
                        continue

                for so in sale_orders:
                    if not so.commission_rule_ids:
                        _logger.info(f"[COMMISSION] Venta {so.name} encontrada, pero no tiene reglas de comisión.")
                        continue

                    # 3. Calcular Ratio
                    if invoice_origin.amount_total == 0:
                        continue
                        
                    ratio = amount_reconciled / invoice_origin.amount_total
                    sign = -1 if is_refund else 1
                    
                    _logger.info(f"[COMMISSION] Calculando: Venta {so.name} | Pago: {amount_reconciled} | Ratio: {ratio}")

                    for rule in so.commission_rule_ids:
                        commission_amount = 0.0
                        
                        # A. Base Manual / Fija
                        if rule.calculation_base == 'manual':
                            commission_amount = rule.fixed_amount * ratio
                        
                        # B. Bases Calculadas
                        else:
                            commission_amount = rule.estimated_amount * ratio

                        if commission_amount != 0:
                            CommissionMove.create({
                                'partner_id': rule.partner_id.id,
                                'sale_order_id': so.id,
                                'invoice_line_id': invoice_origin.invoice_line_ids[0].id if invoice_origin.invoice_line_ids else False,
                                'payment_id': self.env['account.payment'].search([('move_id', '=', payment.id)], limit=1).id,
                                'amount': commission_amount * sign,
                                'base_amount_paid': amount_reconciled * sign,
                                'currency_id': so.currency_id.id,
                                'is_refund': is_refund,
                                'state': 'draft',
                                'name': f"Cmsn: {invoice.name} ({round(ratio*100, 1)}%)"
                            })
                            _logger.info(f"[COMMISSION] GENERADA: {commission_amount} para {rule.partner_id.name}")

            except Exception as e:
                _logger.error(f"[COMMISSION] Error crítico al procesar conciliación ID {rec.id}: {str(e)}")

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        # Disparar cálculo post-conciliación
        res._create_commission_moves()
        return res```

## ./models/commission_move.py
```py
from odoo import models, fields, api

class CommissionMove(models.Model):
    _name = 'commission.move'
    _description = 'Movimiento Individual de Comisión'
    _order = 'date desc, id desc'

    name = fields.Char(string='Referencia', required=True, default='/')
    
    # Relaciones
    partner_id = fields.Many2one('res.partner', string='Comisionista', required=True, index=True)
    sale_order_id = fields.Many2one('sale.order', string='Origen Venta')
    invoice_line_id = fields.Many2one('account.move.line', string='Línea de Factura Origen') # Para tracking preciso
    payment_id = fields.Many2one('account.payment', string='Pago Cliente')
    
    settlement_id = fields.Many2one('commission.settlement', string='Liquidación', ondelete='set null')

    # Datos Económicos
    amount = fields.Monetary(string='Monto Comisión', currency_field='currency_id')
    base_amount_paid = fields.Monetary(string='Base Cobrada', help='Monto del pago cliente que generó esta comisión')
    currency_id = fields.Many2one('res.currency', required=True)
    
    date = fields.Date(default=fields.Date.context_today)
    
    # Estado
    is_refund = fields.Boolean(string='Es Devolución', default=False)
    state = fields.Selection([
        ('draft', 'Pendiente'),
        ('settled', 'En Liquidación'),
        ('invoiced', 'Facturado/Pagado'),
        ('cancel', 'Cancelado')
    ], default='draft', string='Estado', index=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('commission.move') or 'COMM'
        return super().create(vals_list)
```

## ./models/commission_rule.py
```py
from odoo import models, fields, api

class SaleCommissionRule(models.Model):
    _name = 'sale.commission.rule'
    _description = 'Regla de Comisión en Ventas'

    sale_order_id = fields.Many2one('sale.order', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)
    
    role_type = fields.Selection([
        ('internal', 'Vendedor'),
        ('architect', 'Arquitecto'),
        ('construction', 'Constructora'),
        ('referrer', 'Referidor')
    ], string='Rol', required=True, default='internal')

    calculation_base = fields.Selection([
        ('amount_untaxed', 'Monto Base (Subtotal)'),
        ('amount_total', 'Monto Total (Inc. Impuestos)'),
        ('margin', 'Margen (Ganancia)'),
        ('manual', 'Manual / Fijo')
    ], string='Base de Cálculo', default='amount_untaxed', required=True)

    percent = fields.Float(string='Porcentaje %')
    fixed_amount = fields.Monetary(string='Monto Fijo', currency_field='currency_id')
    
    # Estimación
    estimated_amount = fields.Monetary(compute='_compute_estimated', string='Estimado Total')
    currency_id = fields.Many2one(related='sale_order_id.currency_id')

    # CORRECCIÓN: Se eliminó 'sale_order_id.margin' del depends para evitar crash si sale_margin no está instalado
    @api.depends('percent', 'fixed_amount', 'calculation_base', 
                 'sale_order_id.amount_untaxed', 'sale_order_id.amount_total',
                 'sale_order_id.order_line.price_subtotal')
    def _compute_estimated(self):
        for rule in self:
            so = rule.sale_order_id
            amount = 0.0
            
            # Filtrar líneas no comisionables
            # Usamos getattr para evitar errores si el campo no_commission no se ha inicializado aún
            lines = so.order_line.filtered(lambda l: not getattr(l, 'no_commission', False))
            
            if rule.calculation_base == 'manual':
                amount = rule.fixed_amount
            elif rule.calculation_base == 'amount_untaxed':
                base = sum(lines.mapped('price_subtotal'))
                amount = base * (rule.percent / 100.0)
            elif rule.calculation_base == 'amount_total':
                base = sum(lines.mapped('price_total'))
                amount = base * (rule.percent / 100.0)
            elif rule.calculation_base == 'margin':
                # CORRECCIÓN: Verificación segura de existencia del campo margin
                # Si el módulo 'sale_margin' no está instalado, base será 0.0 en lugar de dar error
                base = 0.0
                try:
                    # Intentamos sumar el margen desde las líneas si el campo existe
                    if lines and 'margin' in lines[0]._fields:
                        base = sum(lines.mapped('margin'))
                    # Fallback al margen de la orden si existe
                    elif 'margin' in so._fields:
                        base = so.margin
                except (AttributeError, KeyError):
                    base = 0.0
                
                amount = base * (rule.percent / 100.0)
            
            rule.estimated_amount = amount```

## ./models/commission_settlement.py
```py
from odoo import models, fields, api

class CommissionSettlement(models.Model):
    _name = 'commission.settlement'
    _description = 'Hoja de Liquidación de Comisiones'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Referencia', default='Borrador', copy=False)
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True)
    date = fields.Date(string='Fecha Corte', default=fields.Date.context_today)
    
    move_ids = fields.One2many('commission.move', 'settlement_id', string='Movimientos')
    
    total_amount = fields.Monetary(compute='_compute_totals', string='Total a Pagar', store=True)
    
    # CORRECCIÓN: Se añade default para evitar error NOT NULL en creación manual
    currency_id = fields.Many2one('res.currency', required=True, default=lambda self: self.env.company.currency_id)
    
    vendor_bill_id = fields.Many2one('account.move', string='Factura Proveedor Generada')
    
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('approved', 'Aprobado'),
        ('invoiced', 'Facturado'),
        ('cancel', 'Cancelado')
    ], default='draft', tracking=True)

    @api.depends('move_ids.amount')
    def _compute_totals(self):
        for rec in self:
            rec.total_amount = sum(rec.move_ids.mapped('amount'))

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_create_bill(self):
        self.ensure_one()
        # Obtener parametros de sistema de manera segura
        param_obj = self.env['ir.config_parameter'].sudo()
        prod_id_str = param_obj.get_param('om_advanced_commission.default_commission_product_id')
        journal_id_str = param_obj.get_param('om_advanced_commission.default_commission_journal_id')

        if not prod_id_str or not journal_id_str:
            raise models.ValidationError("Falta configuración. Ve a Ajustes > Ventas > Configuración Comisiones.")

        product_id = int(prod_id_str)
        journal_id = int(journal_id_str)

        invoice_vals = {
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id,
            'invoice_date': fields.Date.today(),
            'journal_id': journal_id,
            'currency_id': self.currency_id.id,
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': product_id,
                    'name': f"Liquidación Comisiones Ref: {self.name}",
                    'quantity': 1,
                    'price_unit': self.total_amount,
                })
            ]
        }
        bill = self.env['account.move'].create(invoice_vals)
        self.vendor_bill_id = bill.id
        self.state = 'invoiced'
        self.move_ids.write({'state': 'invoiced'})
        
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': bill.id,
            'view_mode': 'form',
        }```

## ./models/res_config_settings.py
```py
from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    commission_product_id = fields.Many2one(
        'product.product', 
        string='Producto para Comisiones',
        config_parameter='om_advanced_commission.default_commission_product_id',
        help='Producto de servicio usado al generar facturas de proveedor para comisionistas.'
    )
    commission_journal_id = fields.Many2one(
        'account.journal',
        string='Diario de Comisiones',
        config_parameter='om_advanced_commission.default_commission_journal_id',
        domain=[('type', '=', 'purchase')]
    )
```

## ./models/sale_order.py
```py
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
    no_commission = fields.Boolean(string='Excluir de Comisión')```

## ./security/security.xml
```xml
<odoo>
    <record id="group_commission_manager" model="res.groups">
        <field name="name">Administrador de Comisiones</field>
    </record>
</odoo>```

## ./views/commission_move_views.xml
```xml
<odoo>
    <record id="view_commission_move_list" model="ir.ui.view">
        <field name="name">commission.move.list</field>
        <field name="model">commission.move</field>
        <field name="arch" type="xml">
            <list string="Movimientos" decoration-danger="amount &lt; 0" create="0">
                <field name="date"/>
                <field name="name"/>
                <field name="partner_id"/>
                <field name="sale_order_id"/>
                <field name="amount" sum="Total"/>
                <field name="currency_id" column_invisible="1"/>
                <field name="state" widget="badge" decoration-success="state == 'invoiced'"/>
            </list>
        </field>
    </record>

    <record id="action_commission_move" model="ir.actions.act_window">
        <field name="name">Análisis de Movimientos</field>
        <field name="res_model">commission.move</field>
        <field name="view_mode">list,form,pivot</field>
    </record>
</odoo>
```

## ./views/commission_settlement_views.xml
```xml
<odoo>
    <record id="view_commission_settlement_form" model="ir.ui.view">
        <field name="name">commission.settlement.form</field>
        <field name="model">commission.settlement</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_approve" string="Aprobar" type="object" invisible="state != 'draft'" class="btn-primary"/>
                    <button name="action_create_bill" string="Generar Factura Proveedor" type="object" invisible="state != 'approved'" class="btn-primary"/>
                    <field name="state" widget="statusbar"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1><field name="name"/></h1>
                    </div>
                    <group>
                        <group>
                            <field name="partner_id" readonly="state != 'draft'"/>
                            <field name="date" readonly="state != 'draft'"/>
                        </group>
                        <group>
                            <field name="total_amount"/>
                            <field name="vendor_bill_id" readonly="1"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Detalle de Movimientos">
                            <field name="move_ids" readonly="1">
                                <list>
                                    <field name="sale_order_id"/>
                                    <field name="date"/>
                                    <field name="amount"/>
                                </list>
                            </field>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <record id="action_commission_settlement" model="ir.actions.act_window">
        <field name="name">Liquidaciones</field>
        <field name="res_model">commission.settlement</field>
        <field name="view_mode">list,form</field>
    </record>

    <menuitem id="menu_commission_root" name="Comisiones Pro" parent="sale.sale_menu_root" sequence="40"/>
    <menuitem id="menu_commission_analysis" name="Movimientos Individuales" parent="menu_commission_root" action="action_commission_move" sequence="10"/>
    <menuitem id="menu_commission_settlement" name="Liquidaciones" parent="menu_commission_root" action="action_commission_settlement" sequence="20"/>
</odoo>
```

## ./views/res_config_settings_views.xml
```xml
<odoo>
    <record id="res_config_settings_view_form_commission" model="ir.ui.view">
        <field name="name">res.config.settings.view.form.commission</field>
        <field name="model">res.config.settings</field>
        <field name="inherit_id" ref="sale.res_config_settings_view_form"/>
        <field name="arch" type="xml">
            <xpath expr="//block[@name='catalog_setting_container']" position="after">
                <h2>Configuración Comisiones</h2>
                <div class="row mt16 o_settings_container">
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_right_pane">
                            <label for="commission_product_id"/>
                            <field name="commission_product_id"/>
                            <div class="text-muted">
                                Producto servicio usado en facturas de proveedor.
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_right_pane">
                            <label for="commission_journal_id"/>
                            <field name="commission_journal_id"/>
                        </div>
                    </div>
                </div>
            </xpath>
        </field>
    </record>
</odoo>
```

## ./views/sale_order_views.xml
```xml
<odoo>
    <record id="view_order_form_commission" model="ir.ui.view">
        <field name="name">sale.order.form.commission</field>
        <field name="model">sale.order</field>
        <field name="inherit_id" ref="sale.view_order_form"/>
        <field name="arch" type="xml">
            
            <!-- Botón en la cabecera (Header) -->
            <xpath expr="//header" position="inside">
                <button name="action_recalc_commissions" 
                        string="Recalcular Comisiones" 
                        type="object" 
                        class="btn-secondary"
                        groups="om_advanced_commission.group_commission_manager"
                        invisible="state not in ['sale', 'done']"/>
            </xpath>

            <!-- Pestaña de Configuración de Reglas -->
            <xpath expr="//notebook" position="inside">
                <page string="Gestión de Comisiones" name="commissions_rules">
                    <field name="commission_rule_ids">
                        <list string="Reglas de Comisión" editable="bottom">
                            <field name="role_type"/>
                            <field name="partner_id"/>
                            <field name="calculation_base"/>
                            <field name="percent" invisible="calculation_base == 'manual'"/>
                            <field name="fixed_amount" invisible="calculation_base != 'manual'"/>
                            <field name="estimated_amount" readonly="1" sum="Total Estimado"/>
                            <field name="currency_id" column_invisible="1"/>
                        </list>
                    </field>
                </page>
            </xpath>
            
            <!-- Checkbox en Líneas para exclusión -->
            <xpath expr="//field[@name='order_line']/list//field[@name='price_unit']" position="after">
                <field name="no_commission" optional="hide"/>
            </xpath>
        </field>
    </record>
</odoo>```

## ./wizard/__init__.py
```py
from . import commission_make_invoice
```

## ./wizard/commission_make_invoice.py
```py
from odoo import models, fields, api

class CommissionMakeInvoice(models.TransientModel):
    _name = 'commission.make.invoice'
    _description = 'Asistente para Generar Liquidación'

    date_to = fields.Date(string='Hasta fecha', default=fields.Date.context_today)
    partner_ids = fields.Many2many('res.partner', string='Comisionistas')

    def action_generate_settlements(self):
        """ Busca movimientos draft y los agrupa en Settlements """
        Move = self.env['commission.move']
        Settlement = self.env['commission.settlement']
        
        domain = [('state', '=', 'draft'), ('date', '<=', self.date_to)]
        if self.partner_ids:
            domain.append(('partner_id', 'in', self.partner_ids.ids))
            
        moves = Move.search(domain)
        
        # Agrupar por Partner y Moneda
        grouped = {}
        for m in moves:
            key = (m.partner_id, m.currency_id)
            if key not in grouped:
                grouped[key] = Move
            grouped[key] += m
            
        created_settlements = Settlement
        for (partner, currency), partner_moves in grouped.items():
            settlement = Settlement.create({
                'partner_id': partner.id,
                'currency_id': currency.id,
                'name': f"LIQ-{fields.Date.today()}-{partner.name}",
                'state': 'draft',
                'move_ids': [(6, 0, partner_moves.ids)]
            })
            partner_moves.write({'state': 'settled'})
            created_settlements += settlement
            
        return {
            'type': 'ir.actions.act_window',
            'name': 'Liquidaciones Generadas',
            'res_model': 'commission.settlement',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_settlements.ids)],
        }
```

## ./wizard/commission_make_invoice_views.xml
```xml
<odoo>
    <record id="view_commission_make_invoice" model="ir.ui.view">
        <field name="name">commission.make.invoice.form</field>
        <field name="model">commission.make.invoice</field>
        <field name="arch" type="xml">
            <form>
                <group>
                    <field name="date_to"/>
                    <field name="partner_ids" widget="many2many_tags" placeholder="Dejar vacío para todos"/>
                </group>
                <footer>
                    <button name="action_generate_settlements" string="Generar Liquidaciones" type="object" class="btn-primary"/>
                    <button string="Cancelar" special="cancel" class="btn-secondary"/>
                </footer>
            </form>
        </field>
    </record>

    <record id="action_commission_wizard" model="ir.actions.act_window">
        <field name="name">Generar Liquidación Masiva</field>
        <field name="res_model">commission.make.invoice</field>
        <field name="target">new</field>
        <field name="view_mode">form</field>
    </record>

    <menuitem id="menu_commission_wizard" name="Generar Liquidación Masiva" parent="menu_commission_root" action="action_commission_wizard" sequence="5"/>
</odoo>
```

