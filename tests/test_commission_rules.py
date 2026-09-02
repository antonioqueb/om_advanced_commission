# -*- coding: utf-8 -*-
"""Pruebas del motor de comisiones (regla de negocio del 2 sep 2026).

Se verifica con números redondos que:
* todo se calcula SIN IVA;
* los vendedores comisionan sobre bienes + servicios MENOS las comisiones
  externas del mismo cobro;
* los externos (embajador…) comisionan SOLO sobre bienes, salvo que en
  Ajustes se marque su rol como "comisiona sobre servicios";
* un cobro parcial prorratea todo;
* la fecha de cobro es la del pago (no la de la factura);
* un cobro anterior al inicio de comisiones nace Pagado (fuera del sistema);
* una nota de crédito con devolución de dinero genera comisión negativa.
"""
from datetime import date, timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'commission')
class TestCommissionRules(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_notrack=True,
                                       mail_create_nolog=True, mail_create_nosubscribe=True))
        cls.company = cls.env.company
        cls.cur = cls.company.currency_id
        icp = cls.env['ir.config_parameter'].sudo()
        # Inicio de comisiones: hace un año (para que las pruebas "vivas" no
        # caigan antes del corte) y solo vendedores sobre servicios.
        cls.start = date.today() - timedelta(days=365)
        icp.set_param('om_advanced_commission.start_date', fields.Date.to_string(cls.start))
        icp.set_param('om_advanced_commission.service_roles', 'internal')
        icp.set_param('om_advanced_commission.seller_max_percent', '2.5')
        icp.set_param('om_advanced_commission.external_max_percent', '0')

        cls.tax16 = cls.env['account.tax'].create({
            'name': 'IVA 16 (prueba comisiones)', 'amount': 16.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': cls.company.id,
        })
        cls.goods = cls.env['product.product'].create({
            'name': 'Placa de mármol (prueba)', 'type': 'consu', 'list_price': 1000.0,
            'taxes_id': [(6, 0, cls.tax16.ids)],
        })
        cls.service = cls.env['product.product'].create({
            'name': 'Servicio de manejo de materiales (prueba)', 'type': 'service', 'list_price': 100.0,
            'taxes_id': [(6, 0, cls.tax16.ids)],
        })
        cls.customer = cls.env['res.partner'].create({'name': 'Cliente comisiones (prueba)'})
        cls.architect = cls.env['res.partner'].create({'name': 'Arquitecto embajador (prueba)'})

        group = cls.env.ref('sales_team.group_sale_salesman')
        cls.seller_user = cls.env['res.users'].create({
            'name': 'Vendedor comisiones (prueba)', 'login': 'vendedor_comisiones_prueba',
            'group_ids': [(6, 0, [group.id, cls.env.ref('base.group_user').id])],
        })
        cls.seller = cls.seller_user.partner_id
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Administrador comisiones (prueba)', 'login': 'admin_comisiones_prueba',
            'group_ids': [(6, 0, [group.id, cls.env.ref('base.group_user').id,
                                  cls.env.ref('om_advanced_commission.group_commission_manager').id,
                                  cls.env.ref('account.group_account_invoice').id])],
        })

        # Diario bancario con cuenta de pagos configurada (entrada y salida):
        # sin ella el pago queda in_process SIN asiento y no hay conciliación.
        journals = cls.env['account.journal'].search(
            [('type', 'in', ('bank', 'cash')), ('company_id', '=', cls.company.id)])

        def usable(j):
            return (any(m.payment_account_id for m in j.inbound_payment_method_line_ids)
                    and any(m.payment_account_id for m in j.outbound_payment_method_line_ids))

        cls.bank_journal = journals.filtered(usable)[:1]
        if not cls.bank_journal:
            raise cls.skipTest(cls, 'Sin diario bancario con cuentas de pago configuradas')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _order(self, goods=100000.0, services=20000.0, architect_pct=0.0, seller_pct=2.5):
        lines = [(0, 0, {'product_id': self.goods.id, 'product_uom_qty': 1, 'price_unit': goods,
                         'tax_ids': [(6, 0, self.tax16.ids)]})]
        if services:
            lines.append((0, 0, {'product_id': self.service.id, 'product_uom_qty': 1, 'price_unit': services,
                                 'tax_ids': [(6, 0, self.tax16.ids)]}))
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'user_id': self.seller_user.id,
            'seller1_id': self.seller.id,
            'seller1_percent': seller_pct,
            'order_line': lines,
        })
        if architect_pct:
            self.env['sale.commission.rule'].create({
                'sale_order_id': so.id, 'partner_id': self.architect.id,
                'role_type': 'architect', 'calculation_base': 'amount_untaxed', 'percent': architect_pct,
            })
        so.action_confirm()
        return so

    def _invoice(self, so):
        inv = so._create_invoices()
        inv.invoice_date = fields.Date.today()
        inv.action_post()
        return inv

    def _pay(self, invoice, amount, pay_date=None):
        """Registra y aplica un pago del cliente a la factura."""
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound', 'partner_type': 'customer',
            'partner_id': invoice.partner_id.id, 'amount': amount,
            'date': pay_date or fields.Date.today(), 'journal_id': self.bank_journal.id,
            'currency_id': invoice.currency_id.id,
        })
        payment.action_post()
        recv = (payment.move_id.line_ids | invoice.line_ids).filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
        recv.reconcile()
        return payment

    def _moves(self, so):
        return self.env['commission.move'].sudo().search(
            [('sale_order_id', '=', so.id), ('origin_move_id', '=', False), ('state', '!=', 'cancel')])

    def assertMoney(self, a, b, msg=None):
        self.assertAlmostEqual(a, b, places=1, msg=msg)

    # ------------------------------------------------------------------
    # 1. Vendedor: sin IVA, con servicios, menos comisiones externas
    # ------------------------------------------------------------------
    def test_01_seller_full_payment_goods_services_and_architect(self):
        # 100,000 bienes + 20,000 servicios = 120,000 sin IVA; 139,200 con IVA.
        # Embajador 10 % solo sobre bienes = 10,000.
        # Vendedor 2.5 % sobre (120,000 − 10,000) = 2,750.
        so = self._order(goods=100000.0, services=20000.0, architect_pct=10.0)
        self.assertMoney(so.amount_untaxed, 120000.0)
        self.assertMoney(so.amount_total, 139200.0)
        inv = self._invoice(so)
        self._pay(inv, 139200.0)
        moves = self._moves(so)
        self.assertEqual(len(moves), 2)
        seller = moves.filtered(lambda m: m.partner_id == self.seller)
        arch = moves.filtered(lambda m: m.partner_id == self.architect)
        self.assertMoney(arch.amount, 10000.0, 'embajador solo sobre bienes')
        self.assertMoney(arch.commission_base, 100000.0)
        self.assertFalse(arch.includes_services)
        self.assertMoney(seller.amount, 2750.0, 'vendedor sobre 120,000 menos 10,000 de embajador')
        self.assertMoney(seller.commission_base, 110000.0)
        self.assertMoney(seller.external_deducted, 10000.0)
        self.assertTrue(seller.includes_services)
        self.assertMoney(seller.amount_paid_total, 139200.0, 'cobrado con IVA')
        self.assertMoney(seller.base_amount_paid, 120000.0, 'cobrado sin IVA')
        self.assertEqual(seller.state, 'draft')
        self.assertFalse(seller.pre_start)

    # ------------------------------------------------------------------
    # 2. Ajustes: el embajador también comisiona sobre servicios
    # ------------------------------------------------------------------
    def test_02_service_roles_setting(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'om_advanced_commission.service_roles', 'internal,architect')
        so = self._order(goods=100000.0, services=20000.0, architect_pct=10.0)
        inv = self._invoice(so)
        self._pay(inv, 139200.0)
        moves = self._moves(so)
        arch = moves.filtered(lambda m: m.partner_id == self.architect)
        seller = moves.filtered(lambda m: m.partner_id == self.seller)
        self.assertMoney(arch.amount, 12000.0, 'embajador ahora sobre 120,000')
        self.assertTrue(arch.includes_services)
        self.assertMoney(seller.amount, 2700.0, '2.5 % de (120,000 − 12,000)')
        # Nadie sobre servicios: ni el vendedor.
        self.env['ir.config_parameter'].sudo().set_param('om_advanced_commission.service_roles', ' ')
        so2 = self._order(goods=100000.0, services=20000.0)
        inv2 = self._invoice(so2)
        self._pay(inv2, 139200.0)
        seller2 = self._moves(so2)
        self.assertMoney(seller2.amount, 2500.0, 'vendedor solo bienes: 2.5 % de 100,000')
        self.assertFalse(seller2.includes_services)

    # ------------------------------------------------------------------
    # 3. Cobro parcial: todo se prorratea
    # ------------------------------------------------------------------
    def test_03_partial_payment(self):
        so = self._order(goods=100000.0, services=20000.0, architect_pct=10.0)
        inv = self._invoice(so)
        self._pay(inv, 69600.0)   # la mitad del total con IVA
        moves = self._moves(so)
        seller = moves.filtered(lambda m: m.partner_id == self.seller)
        arch = moves.filtered(lambda m: m.partner_id == self.architect)
        self.assertMoney(arch.amount, 5000.0)
        self.assertMoney(seller.amount, 1375.0)
        self.assertMoney(seller.amount_paid_total, 69600.0)
        self.assertMoney(seller.base_amount_paid, 60000.0)
        # Segundo cobro: completa.
        self._pay(inv, 69600.0)
        moves = self._moves(so)
        self.assertEqual(len(moves), 4)
        self.assertMoney(sum(moves.filtered(lambda m: m.partner_id == self.seller).mapped('amount')), 2750.0)
        self.assertMoney(sum(moves.filtered(lambda m: m.partner_id == self.architect).mapped('amount')), 10000.0)

    # ------------------------------------------------------------------
    # 4. Fecha de cobro = fecha del pago; antes del inicio nace pagada
    # ------------------------------------------------------------------
    def test_04_payment_date_and_start_cutoff(self):
        so = self._order(goods=100000.0, services=0.0)
        inv = self._invoice(so)
        pay_date = date.today() - timedelta(days=40)
        self._pay(inv, 116000.0, pay_date=pay_date)
        seller = self._moves(so)
        self.assertEqual(seller.payment_date, pay_date, 'la fecha de cobro es la del pago, no la de la factura')
        self.assertEqual(seller.state, 'draft')
        # Un cobro ANTES del inicio de comisiones nace Pagado · fuera del sistema.
        so2 = self._order(goods=50000.0, services=0.0)
        inv2 = self._invoice(so2)
        old = self.start - timedelta(days=10)
        self._pay(inv2, 58000.0, pay_date=old)
        m2 = self._moves(so2)
        self.assertEqual(m2.payment_date, old)
        self.assertEqual(m2.state, 'invoiced')
        self.assertTrue(m2.external_paid)
        self.assertTrue(m2.pre_start)
        # …y no aparece en el panel del mes del cobro.
        data = self.env['commission.move'].with_user(self.manager_user).get_commission_dashboard_data(month=old.strftime('%Y-%m'))
        self.assertNotIn(m2.id, [r['id'] for r in data['rows']])
        # El periodo del panel se corta por fecha de cobro.
        data = self.env['commission.move'].with_user(self.manager_user).get_commission_dashboard_data(month=pay_date.strftime('%Y-%m'))
        self.assertIn(seller.id, [r['id'] for r in data['rows']])
        row = [r for r in data['rows'] if r['id'] == seller.id][0]
        self.assertMoney(row['gross'], 116000.0)
        self.assertMoney(row['base'], 100000.0)
        self.assertMoney(row['calc_base'], 100000.0)
        self.assertMoney(row['amount'], 2500.0)

    # ------------------------------------------------------------------
    # 5. Nota de crédito con devolución de dinero: comisión negativa
    # ------------------------------------------------------------------
    def test_05_refund_creates_negative_commission(self):
        so = self._order(goods=100000.0, services=0.0)
        inv = self._invoice(so)
        self._pay(inv, 116000.0)
        seller = self._moves(so)
        self.assertMoney(seller.amount, 2500.0)
        # Nota de crédito completa y devolución del dinero al cliente.
        wizard = self.env['account.move.reversal'].with_context(
            active_model='account.move', active_ids=inv.ids).create({
                'journal_id': inv.journal_id.id, 'reason': 'Devolución (prueba)',
                'date': fields.Date.today(),
            })
        res = wizard.reverse_moves()
        refund = self.env['account.move'].browse(res['res_id'])
        if refund.state != 'posted':
            refund.action_post()
        outbound = self.env['account.payment'].create({
            'payment_type': 'outbound', 'partner_type': 'customer',
            'partner_id': self.customer.id, 'amount': 116000.0,
            'date': fields.Date.today(), 'journal_id': self.bank_journal.id,
            'currency_id': refund.currency_id.id,
        })
        outbound.action_post()
        recv = (outbound.move_id.line_ids | refund.line_ids).filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
        recv.reconcile()
        moves = self._moves(so)
        neg = moves.filtered(lambda m: m.is_refund)
        self.assertTrue(neg, 'la devolución genera comisión negativa')
        self.assertMoney(neg.amount, -2500.0)
        self.assertMoney(sum(moves.mapped('amount')), 0.0, 'neto cero: se descuenta lo pagado')

    # ------------------------------------------------------------------
    # 6. Estimado en la orden y "Otras comisiones" sin duplicar vendedores
    # ------------------------------------------------------------------
    def test_06_order_estimates_and_external_list(self):
        so = self._order(goods=100000.0, services=20000.0, architect_pct=10.0)
        self.assertEqual(len(so.commission_rule_ids), 2)
        self.assertEqual(len(so.external_commission_rule_ids), 1, 'abajo solo va el externo')
        self.assertEqual(so.external_commission_rule_ids.partner_id, self.architect)
        internal = so.commission_rule_ids.filtered(lambda r: r.role_type == 'internal')
        external = so.external_commission_rule_ids
        self.assertMoney(external.estimated_amount, 10000.0)
        self.assertMoney(internal.estimated_amount, 2750.0)
        # % externo equivalente sobre el subtotal comisionable (120,000)
        self.assertAlmostEqual(so.total_external_percent, 8.3333, places=3)
