from odoo import models, fields, api


class ResCompany(models.Model):
    _inherit = 'res.company'

    # El diario es POR compañía: antes vivía en ir.config_parameter
    # (om_advanced_commission.default_commission_journal_id), global. El
    # producto de comisión sigue global (los productos son compartidos).
    commission_journal_id = fields.Many2one(
        'account.journal', string='Diario de Comisiones',
        domain="[('type', '=', 'purchase'), ('company_id', '=', id)]",
        help='Diario de compras donde se generan las facturas de proveedor '
             'de las liquidaciones de comisiones de esta compañía.')

    @api.model
    def _som_migrate_commission_journal(self):
        """Idempotente (corre en cada -u): copia el diario del parámetro
        global a las compañías que aún no tienen diario. A la compañía del
        diario se le asigna tal cual; a las demás se les busca un diario de
        compras con el mismo código; si no hay, se deja vacío para que se
        configure en Ajustes."""
        icp = self.env['ir.config_parameter'].sudo()
        raw = icp.get_param('om_advanced_commission.default_commission_journal_id')
        try:
            journal = self.env['account.journal'].sudo().browse(int(raw)).exists() if raw else None
        except (TypeError, ValueError):
            journal = None
        if not journal:
            return True
        Journal = self.env['account.journal'].sudo()
        for company in self.sudo().search([('commission_journal_id', '=', False)]):
            if journal.company_id == company:
                company.commission_journal_id = journal
                continue
            twin = Journal.search([
                ('type', '=', 'purchase'),
                ('company_id', '=', company.id),
                ('code', '=', journal.code),
            ], limit=1)
            if twin:
                company.commission_journal_id = twin
        return True
