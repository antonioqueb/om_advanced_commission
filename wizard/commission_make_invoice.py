from odoo import models, fields, api


class CommissionMakeInvoice(models.TransientModel):
    _name = 'commission.make.invoice'
    _description = 'Asistente para Generar Liquidación'

    date_to = fields.Date(string='Hasta fecha', default=fields.Date.context_today)
    partner_ids = fields.Many2many('res.partner', string='Comisionistas')

    def action_generate_settlements(self):
        Move = self.env['commission.move']
        Settlement = self.env['commission.settlement']

        domain = [('state', '=', 'draft'), ('date', '<=', self.date_to)]
        if self.partner_ids:
            domain.append(('partner_id', 'in', self.partner_ids.ids))

        moves = Move.search(domain)

        # Agrupar por (partner_id, currency_id, company_id) usando IDs
        grouped = {}
        for m in moves:
            key = (m.partner_id.id, m.currency_id.id, m.company_id.id)
            if key not in grouped:
                grouped[key] = self.env['commission.move']
            grouped[key] |= m

        created_settlements = Settlement
        for (partner_id, currency_id, company_id), partner_moves in grouped.items():
            partner = self.env['res.partner'].browse(partner_id)
            settlement = Settlement.create({
                'partner_id': partner_id,
                'currency_id': currency_id,
                'company_id': company_id,
                'name': f"LIQ-{fields.Date.today()}-{partner.name}",
                'state': 'draft',
                'move_ids': [(6, 0, partner_moves.ids)],
            })
            partner_moves.write({'state': 'settled'})
            created_settlements |= settlement

        return {
            'type': 'ir.actions.act_window',
            'name': 'Liquidaciones Generadas',
            'res_model': 'commission.settlement',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_settlements.ids)],
        }