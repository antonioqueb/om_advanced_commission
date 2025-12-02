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
