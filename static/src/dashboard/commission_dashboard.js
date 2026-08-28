/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const BASIS = [
    { key: "order", label: "Fecha de venta", hint: "Agrupa por la fecha de la orden de venta" },
    { key: "payment", label: "Fecha de cobro", hint: "Agrupa por la fecha en que el cliente pagó (base de la liquidación)" },
];

export class CommissionDashboard extends Component {
    static template = "om_advanced_commission.CommissionDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.basisOptions = BASIS;
        this.state = useState({
            loading: true,
            month: null,          // 'YYYY-MM'; null = mes actual
            partnerId: false,     // filtro por persona (solo administradores)
            basis: "order",       // 'order' | 'payment'
            scope: (this.props.action && this.props.action.context && this.props.action.context.commission_panel) === "externals" ? "externals" : "sellers",
            data: null,
            showHelp: false,
            showDetail: false,
            showIncidents: false,
            showExternalRole: {},
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
                { month: this.state.month, partner_id: this.state.partnerId || null, basis: this.state.basis, scope: this.state.scope }
            );
            this.state.month = this.state.data.month;
            // El vendedor ve su detalle abierto: es su contenido principal.
            if (!this.state.data.is_authorizer) {
                this.state.showDetail = true;
            }
        } finally {
            this.state.loading = false;
        }
    }

    // ── Navegación de periodo ──
    shiftMonth(delta) {
        const [y, m] = this.state.month.split("-").map(Number);
        const d = new Date(y, m - 1 + delta, 1);
        this.state.month = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
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

    // ── Filtro por persona (administradores) ──
    selectPartner(partnerId) {
        this.state.partnerId = partnerId || false;
        this.state.showDetail = !!partnerId;
        this.load();
    }
    clearPartner() {
        this.selectPartner(false);
    }

    toggleHelp() { this.state.showHelp = !this.state.showHelp; }
    toggleDetail() { this.state.showDetail = !this.state.showDetail; }
    toggleIncidents() { this.state.showIncidents = !this.state.showIncidents; }
    toggleRole(role) { this.state.showExternalRole[role] = !this.state.showExternalRole[role]; }
    isRoleOpen(role) { return !!this.state.showExternalRole[role]; }

    // ── Formato ──
    fmt(value) {
        return (value || 0).toLocaleString("es-MX", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    fmtShort(value) {
        const v = Math.abs(value || 0);
        const sign = (value || 0) < 0 ? "-" : "";
        if (v >= 1000000) return sign + (v / 1000000).toFixed(1) + " M";
        if (v >= 1000) return sign + (v / 1000).toFixed(v >= 100000 ? 0 : 1) + " k";
        return sign + v.toFixed(0);
    }
    pct(value) { return Math.round(value || 0); }
    stateBadge(st) {
        return {
            draft: { label: "Pendiente", cls: "om-comm-badge-draft" },
            settled: { label: "En liquidación", cls: "om-comm-badge-settled" },
            invoiced: { label: "Pagado", cls: "om-comm-badge-paid" },
        }[st] || { label: st, cls: "" };
    }
    kindLabel(kind) {
        return { reversal: "Reversa", adjustment: "Ajuste", refund: "Devolución" }[kind] || "";
    }
    get isExternals() {
        return this.state.scope === "externals";
    }
    get basisHint() {
        const b = BASIS.find((x) => x.key === this.state.basis);
        return b ? b.hint : "";
    }

    // ── Derivados ──
    get composition() {
        const k = this.state.data.kpis;
        const paid = Math.max(k.invoiced, 0), settled = Math.max(k.settled, 0), draft = Math.max(k.draft, 0);
        const total = paid + settled + draft;
        if (!total) return { paid: 0, settled: 0, draft: 0, total: 0 };
        return { paid: (paid / total) * 100, settled: (settled / total) * 100, draft: (draft / total) * 100, total };
    }
    get trendMax() {
        const t = this.state.data.trend || [];
        return Math.max(...t.map((x) => Math.abs(x.total)), 1);
    }
    trendHeight(item) {
        const pct = (Math.abs(item.total) / this.trendMax) * 100;
        return "height:" + Math.max(pct, 4) + "%";
    }
    get sellerMax() {
        const s = this.state.data.sellers || [];
        return Math.max(...s.map((x) => Math.abs(x.total)), 1);
    }
    sellerBar(s) {
        return "width:" + Math.max((Math.abs(s.total) / this.sellerMax) * 100, 0) + "%";
    }
    sellerShare(s) {
        const tot = this.state.data.totals.sellers || 0;
        return tot ? ((s.total / tot) * 100).toFixed(1) + "%" : "0.0%";
    }
    get sellersWithMoves() {
        return (this.state.data.sellers || []).filter((s) => s.count > 0).length;
    }
    memberBar(member, role) {
        const max = Math.max(...role.members.map((m) => Math.abs(m.total)), 1);
        return "width:" + (Math.abs(member.total) / max) * 100 + "%";
    }
    get orderCount() {
        return new Set(this.state.data.rows.filter((r) => r.order_id).map((r) => r.order_id)).size;
    }
    barStyle(pct) { return "width:" + Math.max(pct, 0) + "%"; }

    // ── Navegación ──
    openOrder(row) {
        if (!row.order_id) return;
        this.action.doAction({ type: "ir.actions.act_window", res_model: "sale.order", res_id: row.order_id, views: [[false, "form"]], target: "current" });
    }
    openIncidents() { this.action.doAction("om_advanced_commission.action_commission_incident"); }
    openIncident(id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "commission.incident", res_id: id, views: [[false, "form"]], target: "current" });
    }
    openStatement() { this.action.doAction("om_advanced_commission.action_commission_statement"); }
    printReport() { this.action.doAction("om_advanced_commission.action_commission_report_wizard"); }
}

registry.category("actions").add("om_advanced_commission.dashboard", CommissionDashboard);
