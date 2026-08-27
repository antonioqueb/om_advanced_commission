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
            showHelp: false,
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

    goToMonth(key) {
        if (key === this.state.month) return;
        this.state.month = key;
        this.load();
    }

    setBasis(basis) {
        if (this.state.basis === basis) return;
        this.state.basis = basis;
        this.load();
    }

    onSellerChange(ev) {
        this.state.partnerId = parseInt(ev.target.value) || false;
        this.load();
    }

    selectPartner(partnerId) {
        this.state.partnerId = partnerId || false;
        this.load();
    }

    toggleHelp() {
        this.state.showHelp = !this.state.showHelp;
    }

    // ── Formato ──────────────────────────────────
    fmt(value) {
        return (value || 0).toLocaleString("es-MX", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    }

    fmtShort(value) {
        const v = Math.abs(value || 0);
        const sign = (value || 0) < 0 ? "-" : "";
        if (v >= 1000000) return sign + (v / 1000000).toFixed(1) + " M";
        if (v >= 1000) return sign + (v / 1000).toFixed(v >= 100000 ? 0 : 1) + " k";
        return sign + v.toFixed(0);
    }

    pct(value) {
        return Math.round(value || 0);
    }

    stateBadge(st) {
        return {
            draft: { label: "Pendiente", cls: "om-comm-badge-draft" },
            settled: { label: "En liquidación", cls: "om-comm-badge-settled" },
            invoiced: { label: "Pagado", cls: "om-comm-badge-paid" },
        }[st] || { label: st, cls: "" };
    }

    kindLabel(kind) {
        return {
            reversal: "Reversa",
            adjustment: "Ajuste",
            refund: "Devolución",
        }[kind] || "";
    }

    // ── Derivados ───────────────────────────────
    get composition() {
        const k = this.state.data.kpis;
        const paid = Math.max(k.invoiced, 0);
        const settled = Math.max(k.settled, 0);
        const draft = Math.max(k.draft, 0);
        const total = paid + settled + draft;
        if (!total) return { paid: 0, settled: 0, draft: 0, total: 0 };
        return {
            paid: (paid / total) * 100,
            settled: (settled / total) * 100,
            draft: (draft / total) * 100,
            total,
        };
    }

    get trendMax() {
        const t = this.state.data.trend || [];
        return Math.max(...t.map((x) => Math.abs(x.total)), 1);
    }

    trendHeight(item) {
        const pct = (Math.abs(item.total) / this.trendMax) * 100;
        return "height:" + Math.max(pct, 4) + "%";
    }

    get leaderboard() {
        const byPartner = {};
        for (const r of this.state.data.rows) {
            if (!byPartner[r.partner]) byPartner[r.partner] = { name: r.partner, id: r.partner_id, amount: 0, count: 0 };
            byPartner[r.partner].amount += r.amount;
            byPartner[r.partner].count += 1;
        }
        const list = Object.values(byPartner).sort((a, b) => b.amount - a.amount);
        const max = Math.max(...list.map((x) => Math.abs(x.amount)), 1);
        return list.slice(0, 8).map((x) => ({ ...x, pct: (Math.abs(x.amount) / max) * 100 }));
    }

    get orderCount() {
        return new Set(this.state.data.rows.filter((r) => r.order_id).map((r) => r.order_id)).size;
    }

    barStyle(pct) {
        return "width:" + Math.max(pct, 0) + "%";
    }

    // ── Navegación ─────────────────────────────
    openOrder(row) {
        if (!row.order_id) return;
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.order",
            res_id: row.order_id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openIncidents() {
        this.action.doAction("om_advanced_commission.action_commission_incident");
    }

    openIncident(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "commission.incident",
            res_id: id,
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
