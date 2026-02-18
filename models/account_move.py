from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def _create_commission_moves(self):
        CommissionMove = self.env['commission.move'].sudo()

        for rec in self:
            try:
                debit_move = rec.debit_move_id
                credit_move = rec.credit_move_id

                invoice = debit_move.move_id
                payment = credit_move.move_id

                if invoice.move_type not in ('out_invoice', 'out_refund'):
                    invoice, payment = payment, invoice
                    debit_move, credit_move = credit_move, debit_move

                if invoice.move_type not in ('out_invoice', 'out_refund'):
                    _logger.debug(f"[COMM] partial {rec.id}: no es factura cliente, skip")
                    continue

                # La línea de la factura en el partial debe ser receivable
                invoice_line = debit_move if debit_move.move_id == invoice else credit_move
                if invoice_line.account_id.account_type != 'asset_receivable':
                    _logger.debug(f"[COMM] partial {rec.id}: cuenta no receivable ({invoice_line.account_id.account_type}), skip")
                    continue

                is_refund = invoice.move_type == 'out_refund'
                invoice_origin = invoice.reversed_entry_id if is_refund and invoice.reversed_entry_id else invoice

                if invoice_origin.move_type not in ('out_invoice', 'out_refund'):
                    continue

                company = invoice_origin.company_id
                company_currency = company.currency_id
                invoice_currency = invoice_origin.currency_id

                if invoice_currency == company_currency:
                    amount_reconciled_inv_currency = rec.amount
                else:
                    if invoice_line == debit_move:
                        amount_reconciled_inv_currency = (
                            abs(rec.debit_amount_currency)
                            if hasattr(rec, 'debit_amount_currency') and rec.debit_amount_currency
                            else rec.amount
                        )
                    else:
                        amount_reconciled_inv_currency = (
                            abs(rec.credit_amount_currency)
                            if hasattr(rec, 'credit_amount_currency') and rec.credit_amount_currency
                            else rec.amount
                        )

                invoice_total = invoice_origin.amount_total
                if invoice_total == 0:
                    continue

                payment_ratio = min(amount_reconciled_inv_currency / invoice_total, 1.0)
                invoice_untaxed_mxn = abs(invoice_origin.amount_untaxed_signed)
                paid_base_mxn = invoice_untaxed_mxn * payment_ratio

                _logger.info(f"[COMM] partial {rec.id}: factura={invoice_origin.name}, ratio={round(payment_ratio,4)}, base_mxn={round(paid_base_mxn,2)}")

                # --- Buscar SOs relacionadas ---
                # Método 1: via líneas de factura -> sale_line_ids
                sale_orders = self.env['sale.order'].browse()
                try:
                    sale_lines = invoice_origin.invoice_line_ids.mapped('sale_line_ids')
                    if sale_lines:
                        sale_orders = sale_lines.mapped('order_id').filtered(lambda so: so.commission_rule_ids)
                except Exception:
                    pass

                # Método 2: via sale_order.invoice_ids (Many2many en sale.order)
                if not sale_orders:
                    sale_orders = self.env['sale.order'].search([
                        ('invoice_ids', 'in', [invoice_origin.id]),
                        ('commission_rule_ids', '!=', False),
                    ])
                    _logger.info(f"[COMM] partial {rec.id}: fallback SO search result: {sale_orders.mapped('name')}")

                # Método 3: via líneas de la factura -> order_id en sale.order.line
                if not sale_orders:
                    inv_line_ids = invoice_origin.invoice_line_ids.ids
                    if inv_line_ids:
                        sol_ids = self.env['sale.order.line'].sudo().search([
                            ('invoice_lines', 'in', inv_line_ids)
                        ])
                        if sol_ids:
                            candidate_sos = sol_ids.mapped('order_id').filtered(lambda so: so.commission_rule_ids)
                            if candidate_sos:
                                sale_orders = candidate_sos
                                _logger.info(f"[COMM] partial {rec.id}: método3 SOs: {sale_orders.mapped('name')}")

                if not sale_orders:
                    _logger.warning(f"[COMM] partial {rec.id}: sin SOs con reglas de comisión, skip")
                    continue

                _logger.info(f"[COMM] partial {rec.id}: SOs a procesar: {sale_orders.mapped('name')}")

                # --- Peso de cada SO dentro de la factura ---
                so_weights = {}
                total_weight = 0.0
                for so in sale_orders:
                    so_inv_lines = invoice_origin.invoice_line_ids.filtered(
                        lambda l, _so=so: l.sale_line_ids & _so.order_line
                    )
                    weight = sum(abs(l.balance) for l in so_inv_lines) if so_inv_lines else 0.0

                    if weight == 0.0:
                        weight = so.currency_id._convert(
                            so.amount_total, company_currency, company,
                            so.date_order or fields.Date.today()
                        )

                    so_weights[so.id] = weight
                    total_weight += weight

                if total_weight == 0:
                    _logger.warning(f"[COMM] partial {rec.id}: total_weight=0, skip")
                    continue

                payment_rec = self.env['account.payment'].search(
                    [('move_id', '=', payment.id)], limit=1
                )

                sign = -1 if is_refund else 1

                for so in sale_orders:
                    so_ratio = so_weights[so.id] / total_weight
                    so_paid_base = paid_base_mxn * so_ratio

                    so_inv_lines = invoice_origin.invoice_line_ids.filtered(
                        lambda l, _so=so: l.sale_line_ids & _so.order_line
                    )
                    best_inv_line = so_inv_lines[:1] if so_inv_lines else invoice_origin.invoice_line_ids[:1]

                    for rule in so.commission_rule_ids:
                        already = CommissionMove.search_count([
                            ('partial_reconcile_id', '=', rec.id),
                            ('partner_id', '=', rule.partner_id.id),
                            ('sale_order_id', '=', so.id),
                        ], limit=1)
                        if already:
                            _logger.info(f"[COMM] ya existe comisión partial={rec.id} partner={rule.partner_id.id} SO={so.id}, skip")
                            continue

                        if rule.calculation_base == 'manual':
                            rule_amount_mxn = rule.currency_id._convert(
                                rule.fixed_amount, company_currency, company,
                                so.date_order or fields.Date.today()
                            )
                        else:
                            rule_amount_mxn = so.currency_id._convert(
                                rule.estimated_amount, company_currency, company,
                                so.date_order or fields.Date.today()
                            )

                        so_total_mxn = so.currency_id._convert(
                            so.amount_total, company_currency, company,
                            so.date_order or fields.Date.today()
                        )
                        if so_total_mxn == 0:
                            _logger.warning(f"[COMM] SO {so.id} amount_total=0, skip")
                            continue

                        paid_total_mxn_so = abs(invoice_origin.amount_total_signed) * payment_ratio * so_ratio
                        final_ratio = paid_total_mxn_so / so_total_mxn
                        commission_amount = rule_amount_mxn * final_ratio * sign

                        _logger.info(f"[COMM] SO={so.name} rule={rule.id}: estimated={rule.estimated_amount}, rule_mxn={round(rule_amount_mxn,2)}, final_ratio={round(final_ratio,4)}, commission={round(commission_amount,2)}")

                        if abs(commission_amount) < 0.01:
                            _logger.warning(f"[COMM] commission_amount={commission_amount} < 0.01, skip")
                            continue

                        CommissionMove.create({
                            'partner_id': rule.partner_id.id,
                            'sale_order_id': so.id,
                            'invoice_line_id': best_inv_line.id if best_inv_line else False,
                            'payment_id': payment_rec.id if payment_rec else False,
                            'partial_reconcile_id': rec.id,
                            'company_id': company.id,
                            'amount': commission_amount,
                            'base_amount_paid': so_paid_base * sign,
                            'currency_id': company_currency.id,
                            'is_refund': is_refund,
                            'state': 'draft',
                            'name': f"Cmsn: {invoice.name} / {so.name} ({round(final_ratio * 100, 1)}%)",
                        })
                        _logger.info(f"[COMM] ✅ Creada comisión {so.name} partner={rule.partner_id.id}: {round(commission_amount,2)}")

            except Exception as e:
                _logger.error(f"[COMMISSION] Error en partial {rec.id}: {e}", exc_info=True)

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        res.sudo()._create_commission_moves()
        return res