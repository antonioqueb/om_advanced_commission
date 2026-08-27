from odoo import models, api, fields
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _commission_fallback_order(self):
        """Orden de venta cuando las líneas de la factura no traen sale_line_ids
        (nota de crédito manual, factura capturada a mano con origen)."""
        self.ensure_one()
        SO = self.env['sale.order'].sudo()
        if self.reversed_entry_id:
            sls = self.reversed_entry_id.invoice_line_ids.mapped('sale_line_ids')
            if sls:
                return sls.mapped('order_id')[:1]
            origin = self.reversed_entry_id
            if origin.invoice_origin:
                so = SO.search([('name', 'in', [n.strip() for n in origin.invoice_origin.split(',')]),
                                ('company_id', '=', self.company_id.id)], limit=1)
                if so:
                    return so
        if self.invoice_origin:
            return SO.search([('name', 'in', [n.strip() for n in self.invoice_origin.split(',')]),
                              ('company_id', '=', self.company_id.id)], limit=1)
        return SO.browse()

    def _commission_orders(self):
        """Órdenes (con reglas) a las que esta factura de cliente comisiona."""
        self.ensure_one()
        sos = self.invoice_line_ids.mapped('sale_line_ids.order_id')
        if not sos:
            sos = self._commission_fallback_order()
        return sos.filtered(lambda s: s.commission_rule_ids)


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    # ------------------------------------------------------------------
    # Identificación factura / contraparte
    # ------------------------------------------------------------------
    def _commission_invoice_side(self):
        """(factura_cliente, asiento_contraparte, línea_de_factura, línea_contraparte) o None."""
        self.ensure_one()
        debit, credit = self.debit_move_id, self.credit_move_id
        for inv_line, other_line in ((debit, credit), (credit, debit)):
            move = inv_line.move_id
            if move.move_type in ('out_invoice', 'out_refund'):
                if inv_line.account_id.account_type != 'asset_receivable':
                    return None
                # Factura contra su nota de crédito: no entra ni sale dinero,
                # no hay comisión positiva ni negativa que devengar.
                other = other_line.move_id
                if other.move_type in ('out_invoice', 'out_refund'):
                    return None
                # Diferencia cambiaria: Odoo la concilia contra la factura con
                # amount_currency = 0. No es cobro; con el fallback viejo
                # (pesos de la diferencia ÷ dólares de la factura) duplicaba
                # comisión en facturas USD.
                if self._commission_is_exchange_move(other):
                    return None
                return move, other, inv_line, other_line
        return None

    def _commission_is_exchange_move(self, move):
        company = move.company_id
        fx_journal = company.currency_exchange_journal_id if 'currency_exchange_journal_id' in company._fields else False
        if fx_journal and move.journal_id == fx_journal:
            return True
        if 'exchange_move_id' in self._fields and self.env['account.partial.reconcile'].sudo().search_count(
                [('exchange_move_id', '=', move.id)], limit=1):
            return True
        return False

    def _commission_payment_ratio(self, invoice, inv_line):
        """Parte del total de la factura (en su moneda) que cubre este partial."""
        self.ensure_one()
        company_cur = invoice.company_id.currency_id
        if invoice.currency_id == company_cur:
            reconciled = self.amount
        else:
            # Multimoneda: la fracción cobrada SIEMPRE en la moneda de la
            # factura. Jamás caer al monto en pesos (mezcla divisas).
            if inv_line == self.debit_move_id:
                reconciled = abs(self.debit_amount_currency or 0.0)
            else:
                reconciled = abs(self.credit_amount_currency or 0.0)
            if not reconciled:
                return 0.0
        total = invoice.amount_total
        if not total:
            return 0.0
        return min(reconciled / total, 1.0)

    def _commission_cash_received_company(self, other_line):
        """Pesos (moneda compañía) EFECTIVAMENTE recibidos en este partial,
        valuados por el lado del pago (su propio tipo de cambio), no por el
        de la factura."""
        self.ensure_one()
        ccur = other_line.company_id.currency_id
        if other_line == self.debit_move_id:
            amt_cur = abs(self.debit_amount_currency or 0.0)
        else:
            amt_cur = abs(self.credit_amount_currency or 0.0)
        if not other_line.currency_id or other_line.currency_id == ccur:
            return amt_cur or self.amount
        if other_line.amount_currency:
            rate = abs(other_line.balance) / abs(other_line.amount_currency)
            return amt_cur * rate
        return self.amount

    @api.model
    def _commission_base_on_cash_received(self):
        """True (default): base = pesos recibidos al TC del pago.
        False: base = pesos facturados al TC de la factura."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'om_advanced_commission.base_on_cash_received', 'True')
        return str(raw).strip().lower() not in ('false', '0', 'no', '')

    # ------------------------------------------------------------------
    # Devengo
    # ------------------------------------------------------------------
    def _create_commission_moves(self, refresh=False):
        """refresh=True: además de crear lo faltante, alinea lo existente al
        monto que hoy dan las reglas/autorizaciones (ajustes por delta)."""
        for rec in self:
            try:
                rec.sudo()._commission_process(refresh=refresh)
            except Exception as e:  # noqa: BLE001 — jamás tumbar la conciliación
                _logger.error("[COMMISSION] Error en partial %s: %s", rec.id, e, exc_info=True)
                try:
                    rec.sudo()._commission_report_error(e)
                except Exception:  # noqa: BLE001
                    _logger.exception("[COMMISSION] No se pudo reportar el error del partial %s", rec.id)

    def _commission_process(self, refresh=False):
        self.ensure_one()
        CommissionMove = self.env['commission.move'].sudo()

        side = self._commission_invoice_side()
        if not side:
            return
        invoice, counterpart, inv_line, other_line = side
        if invoice.state != 'posted':
            return

        is_refund = invoice.move_type == 'out_refund'
        company = invoice.company_id
        ccur = company.currency_id
        ratio = self._commission_payment_ratio(invoice, inv_line)
        if ratio <= 0:
            return

        # SIEMPRE EN PESOS: en facturas en divisa la base son los pesos que
        # realmente entraron (TC del pago). mxn_factor reescala los pesos de
        # la factura (TC de facturación) a los pesos recibidos. = 1 en MXN.
        mxn_factor = 1.0
        if invoice.currency_id != ccur and self._commission_base_on_cash_received():
            inv_paid_company = abs(invoice.amount_total_signed) * ratio
            cash_company = self._commission_cash_received_company(other_line)
            if inv_paid_company and cash_company:
                mxn_factor = cash_company / inv_paid_company

        # ── Base por LÍNEA de factura (moneda compañía, tipo de cambio de la
        # factura), agrupada por orden de venta. Facturas parciales, multi-
        # orden, precios ajustados al facturar y notas de crédito quedan
        # resueltos aquí sin repartos por peso.
        by_so = {}
        fallback_so = None
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            sale_line = line.sale_line_ids[:1]
            so = sale_line.order_id
            if not so:
                if fallback_so is None:
                    fallback_so = invoice._commission_fallback_order()
                so = fallback_so
            if not so:
                continue
            base_untaxed = abs(line.balance) * ratio * mxn_factor
            tax_factor = (line.price_total / line.price_subtotal) if line.price_subtotal else 1.0
            by_so.setdefault(so, []).append((sale_line or None, base_untaxed, base_untaxed * tax_factor, line))

        if not by_so:
            _logger.debug("[COMM] partial %s: factura %s sin líneas ligadas a ventas", self.id, invoice.name)
            return

        payment_rec = self.env['account.payment'].search([('move_id', '=', counterpart.id)], limit=1)
        sign = -1 if is_refund else 1

        for so, entries in by_so.items():
            if not so.commission_rule_ids:
                continue
            paid_lines = [(sl, bu, bt) for (sl, bu, bt, _l) in entries]
            amounts = so._commission_amounts_for_payment(
                paid_lines, ccur, apply_factors=True, date=invoice.invoice_date or invoice.date)
            paid_base = sum(bu for (sl, bu, _bt) in paid_lines if not (sl and sl.no_commission))
            first_line = entries[0][3]
            seller_factor, ext_factor = so._commission_factors()

            for rule, amount in amounts.items():
                commission_amount = ccur.round(amount * sign)
                factor = seller_factor if rule.role_type == 'internal' else ext_factor
                snapshot = {
                    'rule_id': rule.id,
                    'rule_role': rule.role_type,
                    'rule_base': rule.calculation_base,
                    'rule_percent': rule.percent if rule.calculation_base != 'manual' else 0.0,
                    'rule_fixed_amount': rule.fixed_amount if rule.calculation_base == 'manual' else 0.0,
                    'retention_factor': factor,
                }
                # Ya devengado para esta regla, o movimiento LEGADO (sin
                # snapshot de regla) de la misma conciliación: no duplicar.
                originals = CommissionMove.search([
                    ('partial_reconcile_id', '=', self.id),
                    ('partner_id', '=', rule.partner_id.id),
                    ('sale_order_id', '=', so.id),
                    ('rule_id', 'in', [rule.id, False]),
                    ('origin_move_id', '=', False),
                    ('state', '!=', 'cancel'),
                ])
                if originals:
                    if refresh:
                        originals[0]._commission_apply_expected(
                            commission_amount, snapshot, 'reglas o autorización actualizadas')
                    continue
                if abs(commission_amount) < 0.005:
                    continue
                CommissionMove.create({
                    'partner_id': rule.partner_id.id,
                    'sale_order_id': so.id,
                    'invoice_id': invoice.id,
                    'invoice_line_id': first_line.id,
                    'payment_id': payment_rec.id if payment_rec else False,
                    'partial_reconcile_id': self.id,
                    'company_id': company.id,
                    'amount': commission_amount,
                    'base_amount_paid': ccur.round(paid_base * sign),
                    'currency_id': ccur.id,
                    'is_refund': is_refund,
                    'state': 'draft',
                    'date': self.max_date or fields.Date.context_today(self),
                    'name': f"Cmsn: {invoice.name} / {so.name} ({round(ratio * 100, 1)}%)",
                    **snapshot,
                })
                _logger.info("[COMM] partial %s → %s %s: %.2f (factor %.3f)",
                             self.id, so.name, rule.partner_id.display_name, commission_amount, factor)

    # ------------------------------------------------------------------
    # Observabilidad: el error deja de ser silencioso
    # ------------------------------------------------------------------
    def _commission_report_error(self, exc):
        self.ensure_one()
        side = self._commission_invoice_side()
        invoice = side[0] if side else None
        body = ("⚠️ Comisiones: no se pudo calcular la comisión de la conciliación #%s. "
                "Detalle técnico: %s") % (self.id, str(exc)[:500])
        managers = self.env['commission.move']._commission_manager_users()
        targets = self.env['sale.order']
        if invoice:
            targets = invoice._commission_orders()
        if not targets and invoice:
            invoice.sudo().message_post(body=body, message_type='notification')
            for user in managers:
                invoice.sudo().activity_schedule(
                    'mail.mail_activity_data_todo', user_id=user.id,
                    summary='Comisión no calculada', note=body)
        for so in targets:
            so.sudo().message_post(body=body, message_type='notification')
            for user in managers:
                so.sudo().activity_schedule(
                    'mail.mail_activity_data_todo', user_id=user.id,
                    summary='Comisión no calculada', note=body)

    # ------------------------------------------------------------------
    # Ciclo de vida simétrico: desconciliar revierte
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        res.sudo()._create_commission_moves()
        return res

    def unlink(self):
        moves = self.env['commission.move'].sudo().search([
            ('partial_reconcile_id', 'in', self.ids)])
        if moves:
            moves._on_partial_unreconciled()
        return super().unlink()
