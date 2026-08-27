from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError


class CommissionSettlement(models.Model):
    _name = 'commission.settlement'
    _description = 'Hoja de Liquidación de Comisiones'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(string='Referencia', default='Borrador', copy=False)
    partner_id = fields.Many2one('res.partner', string='Beneficiario', required=True,
                                 domain=lambda self: self.env['res.partner']._commission_beneficiary_domain())
    company_id = fields.Many2one('res.company', string='Compañía', required=True, default=lambda self: self.env.company)
    date = fields.Date(string='Fecha Corte', default=fields.Date.context_today)

    move_ids = fields.One2many('commission.move', 'settlement_id', string='Movimientos')

    total_amount = fields.Monetary(compute='_compute_totals', string='Total a Pagar', store=True)
    currency_id = fields.Many2one('res.currency', required=True, default=lambda self: self.env.company.currency_id)

    payout_method = fields.Selection(related='partner_id.commission_payout_method', string='Forma de Pago', readonly=True)
    vendor_bill_id = fields.Many2one('account.move', string='Factura Proveedor Generada', copy=False)
    payment_reference = fields.Char(string='Referencia de Pago', copy=False,
                                    help='Folio de nómina, transferencia u otro comprobante cuando no se genera factura.')
    paid_date = fields.Date(string='Fecha de Pago', copy=False)

    state = fields.Selection([
        ('draft', 'Borrador'),
        ('approved', 'Aprobado'),
        ('invoiced', 'Pagado'),
        ('cancel', 'Cancelado')
    ], default='draft', tracking=True)

    @api.depends('move_ids.amount')
    def _compute_totals(self):
        for rec in self:
            rec.total_amount = sum(rec.move_ids.mapped('amount'))

    # ------------------------------------------------------------------
    def action_approve(self):
        Incident = self.env['commission.incident']
        for rec in self:
            if rec.state != 'draft':
                raise UserError("Solo se aprueban liquidaciones en borrador.")
            blocked = Incident._blocked_commission_moves(rec.move_ids)
            if blocked:
                raise UserError(
                    "Hay cobros con incidencia abierta en esta liquidación (%s). "
                    "Resuélvelas en Comisiones › Incidencias o quita esos movimientos."
                    % ', '.join(blocked.mapped('name')))
            if rec.currency_id.compare_amounts(rec.total_amount, 0.0) <= 0:
                raise UserError(
                    "La liquidación %s tiene total %s: un saldo en contra se difiere al siguiente corte, "
                    "no se aprueba." % (rec.name, rec.total_amount))
        self.write({'state': 'approved'})

    def action_cancel(self):
        """Regresa los movimientos a Pendiente. Si ya hay factura de proveedor
        debe estar cancelada o en borrador (nunca se desliga un pago real)."""
        for rec in self:
            if rec.state == 'cancel':
                continue
            if rec.vendor_bill_id and rec.vendor_bill_id.state == 'posted':
                raise UserError(
                    "La factura %s está publicada. Cancélala en contabilidad antes de cancelar la liquidación."
                    % rec.vendor_bill_id.name)
            moves = rec.move_ids
            moves.write({'state': 'draft', 'settlement_id': False})
            rec.message_post(body='Liquidación cancelada; %d movimiento(s) regresan a Pendiente.' % len(moves))
            rec.write({'state': 'cancel'})

    def action_mark_paid(self):
        """Nómina / sin documento: cierra la liquidación con referencia."""
        for rec in self:
            if rec.state != 'approved':
                raise UserError("Aprueba la liquidación antes de marcarla como pagada.")
            if rec.payout_method == 'vendor_bill':
                raise UserError("Este beneficiario se paga con factura de proveedor: usa 'Generar Factura Proveedor'.")
            if not rec.payment_reference:
                raise UserError("Captura la referencia de pago (folio de nómina, transferencia, etc.).")
            rec.write({'state': 'invoiced', 'paid_date': fields.Date.context_today(self)})
            rec.move_ids.write({'state': 'invoiced'})
            rec.message_post(body='Pagada vía %s · ref. %s' % (
                dict(rec.partner_id._fields['commission_payout_method'].selection).get(rec.payout_method),
                rec.payment_reference))

    def action_create_bill(self):
        self.ensure_one()
        if self.state != 'approved':
            raise UserError("Aprueba la liquidación antes de facturar.")
        if self.vendor_bill_id:
            raise ValidationError("Ya existe una factura de proveedor para esta liquidación.")
        if self.payout_method and self.payout_method != 'vendor_bill':
            raise UserError("Este beneficiario no se paga con factura de proveedor (ver ficha del contacto).")

        param_obj = self.env['ir.config_parameter'].sudo()
        journal_id_str = param_obj.get_param('om_advanced_commission.default_commission_journal_id')
        product = self.partner_id.commission_product_id
        if not product:
            prod_id_str = param_obj.get_param('om_advanced_commission.default_commission_product_id')
            try:
                product = self.env['product.product'].browse(int(prod_id_str)).exists() if prod_id_str else None
            except (ValueError, TypeError):
                product = None
        try:
            journal = self.env['account.journal'].browse(int(journal_id_str)).exists() if journal_id_str else None
        except (ValueError, TypeError):
            journal = None

        if not product or not journal:
            raise ValidationError("Falta configuración. Ve a Ajustes > Ventas > Configuración Comisiones "
                                  "(producto y diario) o define el producto en la ficha del beneficiario.")
        if journal.company_id != self.company_id:
            raise ValidationError(f"El diario {journal.name} no pertenece a la compañía {self.company_id.name}.")

        # Impuestos/retenciones: los de proveedor del producto (ISR/IVA para
        # persona física se configuran en el producto del beneficiario).
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id,
            'company_id': self.company_id.id,
            'invoice_date': fields.Date.today(),
            'journal_id': journal.id,
            'currency_id': self.currency_id.id,
            'ref': self.name,
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': f"Liquidación Comisiones Ref: {self.name}",
                'quantity': 1,
                'price_unit': self.total_amount,
            })]
        })
        self.write({'vendor_bill_id': bill.id, 'state': 'invoiced',
                    'paid_date': fields.Date.context_today(self)})
        self.move_ids.write({'state': 'invoiced'})

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': bill.id,
            'view_mode': 'form',
        }

    def unlink(self):
        for rec in self:
            if rec.state not in ('draft', 'cancel'):
                raise UserError("Cancela la liquidación %s antes de borrarla." % rec.name)
            rec.move_ids.write({'state': 'draft', 'settlement_id': False})
        return super().unlink()
