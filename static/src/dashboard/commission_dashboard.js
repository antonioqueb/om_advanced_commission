/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class CommissionDashboard extends Component {
    static template = "om_advanced_commission.CommissionDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            month: null,          // 'YYYY-MM'; null = mes actual
            partnerId: false,     // filtro por vendedor (solo autorizadores)
            basis: "order",       // 'order' = fecha de venta · 'payment' = fecha de cobro
            data: null,
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call(
                "commission.move",
                "get_commission_dashboard_data",
                [],
                {
                    month: this.state.month,
                    partner_id: this.state.partnerId || null,
                    basis: this.state.basis,
                }
            );
            this.state.month = this.state.data.month;
        } finally {
            this.state.loading = false;
        }
    }

    shiftMonth(delta) {
        const [y, m] = this.state.month.split("-").map(Number);
        const d = new Date(y, m - 1 + delta, 1);
        this.state.month =
            d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
        this.load();
    }

    onBasisChange(ev) {
        this.state.basis = ev.target.value === "payment" ? "payment" : "order";
        this.load();
    }

    onSellerChange(ev) {
        this.state.partnerId = parseInt(ev.target.value) || false;
        this.load();
    }

    fmt(value) {
        return (value || 0).toLocaleString("es-MX", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    }

    stateBadge(st) {
        return {
            draft: { label: "Pendiente", cls: "om-comm-badge-draft" },
            settled: { label: "En Liquidación", cls: "om-comm-badge-settled" },
            invoiced: { label: "Pagado", cls: "om-comm-badge-paid" },
        }[st] || { label: st, cls: "" };
    }

    openOrder(row) {
        if (!row.order_id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.order",
            res_id: row.order_id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    printReport() {
        this.action.doAction(
            "om_advanced_commission.action_commission_report_wizard"
        );
    }
}

registry
    .category("actions")
    .add("om_advanced_commission.dashboard", CommissionDashboard);
