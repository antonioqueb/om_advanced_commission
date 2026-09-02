# -*- coding: utf-8 -*-
"""Aplicación AUTOMÁTICA de cobros de cliente a sus facturas.

Regla del cliente (2 sep 2026): todo pago de cliente debe quedar aplicado a
factura; un pago "sin aplicar" es un error de captura, no un estado válido.
Un pago sin aplicar no comisiona (no hay conciliación) y desaparece de la
cuenta del vendedor, así que aquí se corrige solo:

* Al registrar un pago (asistente o pago suelto): lo que sobre después de
  las facturas del asistente se aplica a las demás facturas abiertas del
  cliente, primero las mencionadas en el memo, luego las más antiguas.
* Al publicar una factura de cliente: se le aplican los anticipos (pagos con
  saldo sin aplicar) de ese cliente, los más antiguos primero.
* Cron diario y botón en el Panel de Comisiones como red de seguridad.

Solo misma compañía y misma moneda (nunca se cruzan divisas a ciegas). Lo
que no encuentre factura queda como anticipo y se aplica al facturar.
"""
import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

AUTO_APPLY_PARAM = 'om_advanced_commission.auto_apply_payments'
INVOICE_REF_RE = re.compile(r'[A-Z][A-Z0-9]*/\d{4}/\d+')


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    @api.model
    def _som_auto_apply_enabled(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(AUTO_APPLY_PARAM, 'True')
        return str(raw).strip().lower() not in ('false', '0', 'no', '')

    def _som_receivable_line(self):
        self.ensure_one()
        move = self.sudo().move_id
        if not move or move.state != 'posted':
            return self.env['account.move.line']
        return move.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')[:1]

    def _som_unapplied_amount(self):
        """Saldo del pago aún sin aplicar, en la moneda del pago (>= 0)."""
        self.ensure_one()
        line = self._som_receivable_line()
        if not line or line.reconciled:
            return 0.0
        if self.currency_id and self.currency_id != self.company_id.currency_id:
            return abs(line.amount_residual_currency or 0.0)
        return abs(line.amount_residual or 0.0)

    def _som_open_invoices(self, priority_names=()):
        """Facturas de cliente abiertas del mismo cliente comercial, compañía
        y moneda: primero las mencionadas en el memo, luego las más antiguas
        por vencimiento."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        if not partner:
            return self.env['account.move']
        invoices = self.env['account.move'].sudo().search([
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'not in', ('paid', 'in_payment', 'reversed')),
            ('company_id', '=', self.company_id.id),
            ('currency_id', '=', self.currency_id.id),
            ('commercial_partner_id', '=', partner.id),
            ('amount_residual', '>', 0),
        ], order='invoice_date_due asc, invoice_date asc, id asc')
        names = set(priority_names or ())
        first = invoices.filtered(lambda i: i.name in names)
        return first | (invoices - first)

    def _som_auto_apply(self):
        """Aplica el saldo sin aplicar de cada pago a las facturas abiertas
        del cliente. Devuelve [(pago, factura, importe_aplicado)]."""
        applied = []
        if not self._som_auto_apply_enabled():
            return applied
        AML = self.env['account.move.line'].sudo()
        for pay in self.sudo():
            if pay.payment_type != 'inbound' or pay.partner_type != 'customer':
                continue
            line = pay._som_receivable_line()
            if not line or line.reconciled or pay.currency_id.is_zero(pay._som_unapplied_amount()):
                continue
            names = INVOICE_REF_RE.findall(pay.memo or '')
            for inv in pay._som_open_invoices(names):
                inv_line = inv.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
                    and l.account_id == line.account_id)
                if not inv_line:
                    continue
                before = pay._som_unapplied_amount()
                try:
                    with self.env.cr.savepoint():
                        (line | inv_line).reconcile()
                except Exception as exc:  # noqa: BLE001 - un caso raro no frena los demás
                    _logger.warning('[COBRO AUTO] no se pudo aplicar %s a %s: %s', pay.name, inv.name, exc)
                    continue
                AML.invalidate_model(['reconciled', 'amount_residual', 'amount_residual_currency'])
                after = pay._som_unapplied_amount()
                amount = before - after
                if not pay.currency_id.is_zero(amount):
                    applied.append((pay, inv, amount))
                    _logger.info('[COBRO AUTO] %s aplicado a %s: %.2f', pay.name, inv.name, amount)
                if line.reconciled or pay.currency_id.is_zero(after):
                    break
        for pay, inv, amount in applied:
            try:
                inv.message_post(
                    body='Cobro %s aplicado automáticamente por %s %s.' % (
                        pay.name, pay.currency_id.symbol or '', '{:,.2f}'.format(amount)),
                    message_type='notification')
            except Exception:  # noqa: BLE001
                pass
        return applied

    @api.model
    def _som_unapplied_payments_domain(self, company_ids=None):
        return [
            ('payment_type', '=', 'inbound'), ('partner_type', '=', 'customer'),
            ('state', 'in', ('in_process', 'paid')),
            ('company_id', 'in', company_ids or self.env.companies.ids),
        ]

    @api.model
    def _som_auto_apply_all(self, company_ids=None):
        """Recorre todos los pagos de cliente con saldo sin aplicar y los
        aplica. Lo usan el cron, el botón del panel y la migración."""
        payments = self.sudo().search(self._som_unapplied_payments_domain(company_ids), order='date asc, id asc')
        pending = payments.filtered(lambda p: p.move_id and p.move_id.state == 'posted'
                                    and not p.currency_id.is_zero(p._som_unapplied_amount()))
        return pending._som_auto_apply()

    @api.model
    def _cron_som_auto_apply(self):
        for company in self.env['res.company'].sudo().search([]):
            try:
                applied = self.with_company(company)._som_auto_apply_all([company.id])
                if applied:
                    _logger.info('[COBRO AUTO] %s: %d aplicación(es)', company.name, len(applied))
            except Exception:  # noqa: BLE001
                _logger.exception('[COBRO AUTO] falló en %s', company.name)
        return True

    def action_post(self):
        res = super().action_post()
        # El asistente "Registrar pago" concilia DESPUÉS de publicar: ahí se
        # corre al final del asistente (ver AccountPaymentRegister).
        if not self.env.context.get('som_skip_auto_apply'):
            try:
                self._som_auto_apply()
            except Exception:  # noqa: BLE001 - jamás bloquear el pago
                _logger.exception('[COBRO AUTO] falló tras publicar %s', self.mapped('name'))
        return res


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def _create_payments(self):
        payments = super(AccountPaymentRegister, self.with_context(som_skip_auto_apply=True))._create_payments()
        # Ya se aplicó a las facturas del asistente; lo que sobre va a las
        # demás facturas abiertas del cliente.
        try:
            payments._som_auto_apply()
        except Exception:  # noqa: BLE001
            _logger.exception('[COBRO AUTO] falló tras el asistente de pago')
        return payments


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        invoices = posted.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
        if invoices and not self.env.context.get('som_skip_auto_apply'):
            Payment = self.env['account.payment'].sudo()
            for inv in invoices:
                try:
                    if not Payment._som_auto_apply_enabled():
                        break
                    partner = inv.commercial_partner_id
                    payments = Payment.search([
                        ('payment_type', '=', 'inbound'), ('partner_type', '=', 'customer'),
                        ('state', 'in', ('in_process', 'paid')),
                        ('company_id', '=', inv.company_id.id),
                        ('currency_id', '=', inv.currency_id.id),
                        ('partner_id.commercial_partner_id', '=', partner.id),
                    ], order='date asc, id asc')
                    payments = payments.filtered(
                        lambda p: p.move_id and p.move_id.state == 'posted'
                        and not p.currency_id.is_zero(p._som_unapplied_amount()))
                    if payments:
                        payments.with_context(som_skip_auto_apply=True)._som_auto_apply()
                except Exception:  # noqa: BLE001 - jamás bloquear la factura
                    _logger.exception('[COBRO AUTO] falló al publicar %s', inv.name)
        return posted
