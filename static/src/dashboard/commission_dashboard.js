/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/* Panel de Comisiones.
 * UN SOLO RELOJ: la fecha de cobro. Ya no existe el corte por fecha de la
 * orden: las comisiones se pagan sobre lo que el cliente pagó, no sobre lo
 * que se vendió. Nada anterior al inicio de comisiones (Ajustes) aparece. */
export class CommissionDashboard extends Component {
    static template = "om_advanced_commission.CommissionDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            month: null,          // 'YYYY-MM'; null = mes actual
            partnerId: false,     // filtro por persona (solo administradores)
            scope: (this.props.action && this.props.action.context && this.props.action.context.commission_panel) === "externals" ? "externals" : "sellers",
            data: null,
            showHelp: false,
            showDetail: false,
            showIncidents: false,
            showUnapplied: true,
            showUncommissioned: true,
            applying: false,
            applyResult: null,
            // tabla de personas
            sortKey: "total",
            sortDir: -1,
            search: "",
            expanded: {},
            // detalle
            detailState: "all",
            detailSearch: "",
            detailLimit: 80,
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
                { month: this.state.month, partner_id: this.state.partnerId || null, scope: this.state.scope }
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
        if (delta < 0 && this.state.data && this.state.data.is_start_month) return;
        const [y, m] = this.state.month.split("-").map(Number);
        const d = new Date(y, m - 1 + delta, 1);
        this.state.month = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
        this.load();
    }
    goToMonth(key) {
        if (key === this.state.month) return;
        if (this.state.data && key < this.state.data.start_month) return;
        this.state.month = key;
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
    toggleUnapplied() { this.state.showUnapplied = !this.state.showUnapplied; }
    toggleUncommissioned() { this.state.showUncommissioned = !this.state.showUncommissioned; }

    // ── Tabla de personas: orden, búsqueda, expansión ──
    sortBy(key) {
        if (this.state.sortKey === key) {
            this.state.sortDir = -this.state.sortDir;
        } else {
            this.state.sortKey = key;
            this.state.sortDir = key === "name" || key === "role_label" ? 1 : -1;
        }
    }
    sortIcon(key) {
        if (this.state.sortKey !== key) return "";
        return this.state.sortDir < 0 ? "▼" : "▲";
    }
    onSearch(ev) { this.state.search = ev.target.value || ""; }
    toggleExpand(id) { this.state.expanded[id] = !this.state.expanded[id]; }
    isExpanded(id) { return !!this.state.expanded[id]; }
    get people() {
        const list = (this.state.data.people || this.state.data.sellers || []).slice();
        const q = this.state.search.trim().toLowerCase();
        const filtered = q ? list.filter((p) => (p.name || "").toLowerCase().includes(q) || (p.user_login || "").toLowerCase().includes(q)) : list;
        const k = this.state.sortKey, d = this.state.sortDir;
        filtered.sort((a, b) => {
            const va = a[k], vb = b[k];
            if (typeof va === "string" || typeof vb === "string") return String(va || "").localeCompare(String(vb || "")) * d;
            return ((va || 0) - (vb || 0)) * d;
        });
        return filtered;
    }
    get peopleAll() { return this.state.data.people || this.state.data.sellers || []; }
    get stateColumns() {
        const all = this.peopleAll;
        const has = (key) => all.some((p) => Math.abs(p[key] || 0) > 0.005);
        return { draft: has("draft"), settled: has("settled"), invoiced: has("invoiced"), retained: has("retained"), deducted: has("deducted") };
    }
    get peopleTotals() {
        const t = { total: 0, base: 0, gross: 0, calc_base: 0, deducted: 0, orders: 0, count: 0, draft: 0, settled: 0, invoiced: 0, retained: 0 };
        for (const p of this.people) { for (const k of Object.keys(t)) t[k] += p[k] || 0; }
        const denom = t.calc_base || t.base;
        t.pct = denom ? (t.total / denom) * 100 : 0;
        t.avg_order = t.orders ? t.total / t.orders : 0;
        return t;
    }
    get peopleMax() { return Math.max(...this.peopleAll.map((p) => Math.abs(p.total)), 1); }
    personBar(p) { return "width:" + Math.max((Math.abs(p.total) / this.peopleMax) * 100, 0) + "%"; }
    personShare(p) {
        const tot = this.peopleAll.reduce((a, x) => a + (x.total || 0), 0);
        return tot ? ((p.total / tot) * 100).toFixed(1) + "%" : "0.0%";
    }
    statusStack(p) {
        const tot = Math.max((p.draft || 0) + (p.settled || 0) + (p.invoiced || 0), 0.0001);
        return { draft: ((p.draft || 0) / tot) * 100, settled: ((p.settled || 0) / tot) * 100, invoiced: ((p.invoiced || 0) / tot) * 100 };
    }
    personRows(p) {
        return this.state.data.rows.filter((r) => r.partner_id === p.id).sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount)).slice(0, 8);
    }
    fmtPct(v) { return (v || 0).toFixed(2) + "%"; }

    // ── Variación vs mes anterior ──
    get prevDelta() {
        const t = this.state.data.trend || [];
        const idx = t.findIndex((x) => x.current);
        if (idx <= 0) return null;
        if (t[idx - 1].before_start) return null;
        const cur = t[idx].total, prev = t[idx - 1].total;
        if (!prev) return cur ? { pct: 100, up: true, prevLabel: t[idx - 1].label } : null;
        const pct = ((cur - prev) / Math.abs(prev)) * 100;
        return { pct: Math.abs(pct).toFixed(1), up: pct >= 0, prevLabel: t[idx - 1].label };
    }

    // ── Detalle: filtros ──
    setDetailState(st) { this.state.detailState = st; this.state.detailLimit = 80; }
    onDetailSearch(ev) { this.state.detailSearch = ev.target.value || ""; this.state.detailLimit = 80; }
    get detailRowsAll() {
        const q = this.state.detailSearch.trim().toLowerCase();
        return this.state.data.rows.filter((r) => {
            if (this.state.detailState !== "all" && r.state !== this.state.detailState) return false;
            if (!q) return true;
            return [r.order, r.customer, r.partner, r.invoice, r.name].some((v) => (v || "").toLowerCase().includes(q));
        });
    }
    get detailRows() { return this.detailRowsAll.slice(0, this.state.detailLimit); }
    showMoreDetail() { this.state.detailLimit += 100; }
    detailCount(st) { return this.state.data.rows.filter((r) => st === "all" || r.state === st).length; }
    get detailHasDeducted() { return this.state.data.rows.some((r) => Math.abs(r.deducted || 0) > 0.005); }

    // ── Exportar CSV (tabla de personas) ──
    exportCsv() {
        const head = ["Nombre", "Tipo", "Órdenes", "Movimientos", "Cobrado con IVA", "Cobrado sin IVA", "Externos descontados", "Base de cálculo", "Comisión", "% efectivo", "Promedio por orden", "Pendiente", "En liquidación", "Pagado", "Retenido"];
        const lines = [head.join(",")];
        for (const p of this.people) {
            lines.push([`"${(p.name || "").replace(/"/g, '""')}"`, p.role_label || "", p.orders, p.count, p.gross, p.base, p.deducted, p.calc_base, p.total, p.pct, p.avg_order, p.draft, p.settled, p.invoiced, p.retained].join(","));
        }
        const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `comisiones_${this.isExternals ? "externos" : "vendedores"}_${this.state.month}.csv`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    }

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
    get hasNoCommission() {
        const d = this.state.data;
        return d && d.is_authorizer && !this.isExternals && ((d.unapplied && d.unapplied.count) || (d.uncommissioned && d.uncommissioned.count));
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
    get orderCount() {
        return new Set(this.state.data.rows.filter((r) => r.order_id).map((r) => r.order_id)).size;
    }
    barStyle(pct) { return "width:" + Math.max(pct, 0) + "%"; }

    // ── Navegación ──
    openOrder(row) {
        if (!row.order_id) return;
        this.action.doAction({ type: "ir.actions.act_window", res_model: "sale.order", res_id: row.order_id, views: [[false, "form"]], target: "current" });
    }
    openOrderId(id) {
        if (!id) return;
        this.action.doAction({ type: "ir.actions.act_window", res_model: "sale.order", res_id: id, views: [[false, "form"]], target: "current" });
    }
    openPayment(id) {
        if (!id) return;
        this.action.doAction({ type: "ir.actions.act_window", res_model: "account.payment", res_id: id, views: [[false, "form"]], target: "current" });
    }
    openAudit() { this.action.doAction("om_advanced_commission.action_commission_audit"); }
    async applyPaymentsNow() {
        if (this.state.applying) return;
        this.state.applying = true;
        try {
            const res = await this.orm.call("commission.move", "apply_unapplied_payments", []);
            this.state.applyResult = res;
            await this.load();
        } finally {
            this.state.applying = false;
        }
    }
    openIncidents() { this.action.doAction("om_advanced_commission.action_commission_incident"); }
    openIncident(id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "commission.incident", res_id: id, views: [[false, "form"]], target: "current" });
    }
    openStatement() { this.action.doAction("om_advanced_commission.action_commission_statement"); }
    markPaid(row) {
        this.action.doAction({
            type: "ir.actions.act_window", res_model: "commission.mark.paid.wizard", views: [[false, "form"]], target: "new",
            name: "Marcar como cobrada", context: { default_move_ids: [[6, 0, [row.id]]] },
        }, { onClose: () => this.load() });
    }
    printReport() { this.action.doAction("om_advanced_commission.action_commission_report_wizard"); }
}

registry.category("actions").add("om_advanced_commission.dashboard", CommissionDashboard);
