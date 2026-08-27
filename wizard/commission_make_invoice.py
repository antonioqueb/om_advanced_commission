from odoo import models, fields, api


class CommissionMakeInvoice(models.TransientModel):
    _name = 'commission.make.invoice'
    _description = 'Asistente para Generar Liquidación'

    date_to = fields.Date(string='Hasta fecha (de cobro)', default=fields.Date.context_today,
                          help='Se liquidan los movimientos pendientes con fecha de cobro hasta este día.')
    partner_ids = fields.Many2many('res.partner', string='Comisionistas',
                                   domain=lambda self: self.env['res.partner']._commission_beneficiary_domain())

    def action_generate_settlements(self):
        Move = self.env['commission.move']
        Settlement = self.env['commission.settlement']

        domain = [('state', '=', 'draft'), ('date', '<=', self.date_to)]
        if self.partner_ids:
            domain.append(('partner_id', 'in', self.partner_ids.ids))

        moves = Move.search(domain)

        # Agrupar por (partner_id, currency_id, company_id)
        grouped = {}
        for m in moves:
            key = (m.partner_id.id, m.currency_id.id, m.company_id.id)
            grouped.setdefault(key, Move)
            grouped[key] |= m

        created_settlements = Settlement
        deferred = []
        for (partner_id, currency_id, company_id), partner_moves in grouped.items():
            partner = self.env['res.partner'].browse(partner_id)
            currency = self.env['res.currency'].browse(currency_id)
            total = sum(partner_moves.mapped('amount'))
            # Saldo en contra (reversas/devoluciones > devengado): se difiere,
            # los movimientos siguen pendientes y netean en el siguiente corte.
            if currency.compare_amounts(total, 0.0) <= 0:
                deferred.append('%s (%s)' % (partner.display_name, currency.round(total)))
                continue
            settlement = Settlement.create({
                'partner_id': partner_id,
                'currency_id': currency_id,
                'company_id': company_id,
                'date': self.date_to,
                'name': f"LIQ-{fields.Date.today()}-{partner.name}",
                'state': 'draft',
                'move_ids': [(6, 0, partner_moves.ids)],
            })
            partner_moves.write({'state': 'settled'})
            created_settlements |= settlement

        if deferred:
            for s in created_settlements:
                s.message_post(body='Diferidos por saldo en contra en este corte: %s' % ', '.join(deferred))
        if not created_settlements:
            msg = 'Sin movimientos pendientes que liquidar.'
            if deferred:
                msg = 'Nada que liquidar. Diferidos por saldo en contra: %s' % ', '.join(deferred)
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'Liquidaciones', 'message': msg, 'type': 'warning', 'sticky': bool(deferred)},
            }

        return {
            'type': 'ir.actions.act_window',
            'name': 'Liquidaciones Generadas',
            'res_model': 'commission.settlement',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_settlements.ids)],
        }
