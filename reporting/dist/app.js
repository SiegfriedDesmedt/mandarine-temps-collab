const state = {
  data: null,
  view: "overview",
  period: null,
  bu: "all",
  search: "",
  page: 1,
  projectLifecycle: "active",
  projectDetailPages: { orders: 1, documents: 1, tasks: 1 },
  projectTaskStatus: null,
  selectedProjectId: null,
  sorts: {
    collaborators: { key: "name", direction: "asc" },
    projects: { key: "cost", direction: "desc" },
  },
  columnFilters: {
    collaborators: {},
    projects: {},
  },
  projectGlobalFilters: { type: "Client" },
  collaboratorInsufficientOnly: false,
  adminData: null,
  adminError: null,
  adminLoading: false,
  adminPollTimer: null,
};

const viewMeta = {
  overview: {
    eyebrow: "Pilotage opérationnel",
    title: "Synthèse des Facturations et Coûts",
    subtitle: "Vue consolidée Odoo, Sellsy et référentiels internes",
  },
  collaborators: {
    eyebrow: "Ressources",
    title: "Activité des collaborateurs",
    subtitle: "Temps saisis et répartition par métier",
  },
  projects: {
    eyebrow: "Rentabilité",
    title: "Projets & clients",
    subtitle: "Rapprochement des temps Odoo et des commandes Sellsy",
  },
  admin: {
    eyebrow: "Administration",
    title: "Intégrations & fraîcheur",
    subtitle: "Pilotage des extractions Odoo et des fichiers utilisés par le reporting",
  },
};

const appView = document.querySelector("#app-view");
const periodFilter = document.querySelector("#period-filter");
const buFilter = document.querySelector("#bu-filter");
const searchFilter = document.querySelector("#search-filter");

const numberFormatter = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
const oneDecimalFormatter = new Intl.NumberFormat("fr-FR", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});
const currencyFormatter = new Intl.NumberFormat("fr-FR", {
  style: "currency",
  currency: "EUR",
  maximumFractionDigits: 0,
});
const dateFormatter = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "long",
  year: "numeric",
});

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function normalized(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function formatHours(value) {
  return `${numberFormatter.format(value || 0)} h`;
}

function formatCurrency(value) {
  return currencyFormatter.format(value || 0);
}

function formatPercent(value) {
  return `${oneDecimalFormatter.format(value || 0)} %`;
}

function compactCurrency(value) {
  const absolute = Math.abs(value || 0);
  if (absolute >= 1_000_000) return `${oneDecimalFormatter.format(value / 1_000_000)} M€`;
  if (absolute >= 1_000) return `${numberFormatter.format(value / 1_000)} k€`;
  return `${numberFormatter.format(value)} €`;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "—";
  if (bytes >= 1_000_000) return `${oneDecimalFormatter.format(bytes / 1_000_000)} Mo`;
  if (bytes >= 1_000) return `${numberFormatter.format(bytes / 1_000)} Ko`;
  return `${numberFormatter.format(bytes)} o`;
}

function formatDateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("fr-FR", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function formatIsoDate(value) {
  if (!value) return "—";
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(parsed);
}

function formatMonthLabel(month, style = "short") {
  const parsed = new Date(`${month}-01T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return month;
  return new Intl.DateTimeFormat("fr-FR", { month: style, year: style === "long" ? "numeric" : undefined }).format(parsed);
}

function updateFilterOptions() {
  const fiscalPeriods = state.data.meta.fiscalPeriods || [{
    value: "fy",
    label: state.data.meta.periodLabel,
    months: state.data.meta.months || [],
  }];
  const availableValues = fiscalPeriods.map((period) => period.value);
  if (!state.period || !availableValues.includes(state.period)) {
    state.period = state.data.meta.defaultPeriod || fiscalPeriods.at(-1)?.value || "fy";
  }
  periodFilter.innerHTML = fiscalPeriods
    .slice()
    .reverse()
    .map((period) => `<option value="${esc(period.value)}">${esc(period.label)}</option>`)
    .join("");
  periodFilter.value = state.period;

  const selectedBu = state.bu;
  const businessUnits = [...new Set(state.data.monthlyBu.map((row) => row.bu))].sort((a, b) => a.localeCompare(b, "fr"));
  buFilter.innerHTML = '<option value="all">Toutes les BU</option>'
    + businessUnits.map((bu) => `<option value="${esc(bu)}">${esc(bu)}</option>`).join("");
  state.bu = selectedBu === "all" || businessUnits.includes(selectedBu) ? selectedBu : "all";
  buFilter.value = state.bu;
}

async function loadReportData({ rerender = true } = {}) {
  const response = await fetch(`data.json?ts=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  state.data = await response.json();
  updateFilterOptions();
  if (rerender) render();
}

function formatAge(hours) {
  if (hours === null || hours === undefined) return "Jamais reçu";
  const value = Number(hours);
  if (value < 1) return "Il y a moins d’une heure";
  if (value < 48) return `Il y a ${numberFormatter.format(value)} h`;
  return `Il y a ${numberFormatter.format(value / 24)} j`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const value = Number(seconds);
  if (value < 60) return `${oneDecimalFormatter.format(value)} s`;
  return `${Math.floor(value / 60)} min ${numberFormatter.format(value % 60)} s`;
}

function activeMonths() {
  const fiscalPeriods = state.data.meta.fiscalPeriods || [];
  const selected = fiscalPeriods.find((period) => period.value === state.period);
  return selected?.months || state.data.meta.months || [];
}

function selectedFiscalPeriod() {
  return (state.data.meta.fiscalPeriods || []).find((period) => period.value === state.period) || null;
}

function monthRangeFromDates(startValue, endValue) {
  if (!startValue || !endValue) return [];
  const months = [];
  const cursor = new Date(`${startValue.slice(0, 7)}-01T12:00:00`);
  const end = new Date(`${endValue.slice(0, 7)}-01T12:00:00`);
  while (cursor <= end) {
    months.push(`${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, "0")}`);
    cursor.setMonth(cursor.getMonth() + 1);
  }
  return months;
}

function collaboratorPeriodMonths() {
  const selected = selectedFiscalPeriod();
  if (selected?.fullMonths?.length) return selected.fullMonths;
  const derived = monthRangeFromDates(selected?.start, selected?.end);
  return derived.length ? derived : activeMonths();
}

const timeControlPresentation = {
  conform: { label: "Conforme", className: "", globalLabel: "À jour" },
  watch: { label: "À vérifier", className: "warning", globalLabel: "À vérifier" },
  insufficient: { label: "Insuffisant", className: "danger", globalLabel: "En retard" },
  unevaluated: { label: "Non évalué", className: "archived", globalLabel: "Non évaluable" },
};

function collaboratorPeriodControl(person) {
  const monthKeys = collaboratorPeriodMonths();
  const controls = new Map((person.timeControlMonths || []).map((row) => [row.month, row]));
  const actuals = new Map((person.months || []).map((row) => [row.month, row]));
  const months = monthKeys.map((month) => controls.get(month) || {
    month,
    hours: Number(actuals.get(month)?.hours || 0),
    clientHours: Number(actuals.get(month)?.clientHours || 0),
    internalHours: Number(actuals.get(month)?.internalHours || 0),
    absenceHours: Number(actuals.get(month)?.absenceHours || 0),
    unclassifiedHours: Number(actuals.get(month)?.unclassifiedHours || 0),
    expectedHours: null,
    coverageRate: null,
    missingHours: null,
    status: "unevaluated",
    evaluation: "unavailable",
  });
  const counts = { conform: 0, watch: 0, insufficient: 0, unevaluated: 0 };
  months.forEach((month) => { counts[month.status] = (counts[month.status] || 0) + 1; });
  const evaluatedCount = counts.conform + counts.watch + counts.insufficient;
  const globalStatus = counts.insufficient
    ? "insufficient"
    : counts.watch
      ? "watch"
      : evaluatedCount
        ? "conform"
        : "unevaluated";
  return {
    months,
    monthByKey: new Map(months.map((month) => [month.month, month])),
    totalHours: months.reduce((sum, month) => sum + Number(month.hours || 0), 0),
    missingHours: months.reduce((sum, month) => sum + Number(month.missingHours || 0), 0),
    counts,
    globalStatus,
  };
}

function aggregateMonths(rows, options = {}) {
  const months = new Set(activeMonths());
  const bu = options.bu ?? state.bu;
  return rows
    .filter((row) => months.has(row.month))
    .filter((row) => bu === "all" || row.bu === bu)
    .reduce(
      (sum, row) => {
        sum.hours += Number(row.hours || 0);
        sum.cost += Number(row.cost || 0);
        sum.laborCost += Number(row.laborCost ?? row.cost ?? 0);
        sum.otherCosts += Number(row.otherCosts || 0);
        sum.totalCost += Number(row.totalCost ?? (Number(row.cost || 0) + Number(row.otherCosts || 0)));
        sum.invoiceAmount += Number(row.invoiceAmount ?? row.revenue ?? 0);
        sum.creditAmount += Number(row.creditAmount || 0);
        sum.revenue += Number(row.revenue || 0);
        sum.orders += Number(row.orders || 0);
        return sum;
      },
      { hours: 0, cost: 0, laborCost: 0, otherCosts: 0, totalCost: 0, invoiceAmount: 0, creditAmount: 0, revenue: 0, orders: 0 },
    );
}

function aggregateEntityMonths(entity, { useBu = true } = {}) {
  const months = new Set(activeMonths());
  return (entity.months || [])
    .filter((row) => months.has(row.month))
    .filter((row) => !useBu || state.bu === "all" || row.bu === state.bu || entity.bu === state.bu)
    .reduce(
      (sum, row) => {
        sum.hours += Number(row.hours || 0);
        sum.cost += Number(row.cost || 0);
        sum.revenue += Number(row.revenue || 0);
        sum.fixedCosts += Number(row.fixedCosts || 0);
        Object.entries(row.fixedCostCategories || {}).forEach(([code, value]) => {
          sum.fixedCostCategories[code] = (sum.fixedCostCategories[code] || 0) + Number(value || 0);
        });
        return sum;
      },
      { hours: 0, cost: 0, revenue: 0, fixedCosts: 0, fixedCostCategories: {} },
    );
}

function valueContains(value, filter) {
  return !filter || normalized(value).includes(normalized(filter));
}

function sortRows(rows, view, getters) {
  const sort = state.sorts[view];
  const getter = getters[sort.key] || getters.name;
  const direction = sort.direction === "desc" ? -1 : 1;
  return rows.sort((left, right) => {
    const leftValue = getter(left);
    const rightValue = getter(right);
    if (typeof leftValue === "number" || typeof rightValue === "number") {
      return (Number(leftValue || 0) - Number(rightValue || 0)) * direction;
    }
    return String(leftValue ?? "").localeCompare(String(rightValue ?? ""), "fr", { sensitivity: "base" }) * direction;
  });
}

function sortableHeader(view, key, label) {
  const sort = state.sorts[view];
  const indicator = sort.key === key ? (sort.direction === "asc" ? " ↑" : " ↓") : "";
  return `<button type="button" class="sort-button" data-sort-view="${view}" data-sort-key="${key}">${esc(label)}${indicator}</button>`;
}

function columnFilter(view, key, label) {
  const value = state.columnFilters[view]?.[key] || "";
  return `<input class="column-filter" data-filter-view="${view}" data-filter-key="${key}" value="${esc(value)}" aria-label="Filtrer ${esc(label)}" placeholder="Filtrer…" />`;
}

function filteredCollaborators() {
  const query = state.view === "overview" ? "" : normalized(state.search);
  const filters = state.columnFilters.collaborators;
  const monthKeys = collaboratorPeriodMonths();
  const rows = state.data.collaborators
    .map((person) => {
      const current = aggregateEntityMonths(person, { useBu: false });
      const control = collaboratorPeriodControl(person);
      current.hours = control.totalHours;
      const presentation = timeControlPresentation[control.globalStatus];
      return { ...person, current, control, status: [presentation.globalLabel, presentation.className] };
    })
    .filter((person) => person.control.months.some((month) => month.status !== "unevaluated" || Number(month.hours || 0) !== 0))
    .filter((person) => !query || normalized(`${person.name} ${person.department}`).includes(query))
    .filter((person) => valueContains(person.name, filters.name))
    .filter((person) => valueContains(person.department, filters.department))
    .filter((person) => valueContains(oneDecimalFormatter.format(person.control.totalHours), filters.total))
    .filter((person) => valueContains(person.status[0], filters.status))
    .filter((person) => !state.collaboratorInsufficientOnly || person.control.counts.insufficient > 0);
  const monthGetters = Object.fromEntries(monthKeys.map((month) => [
    `month:${month}`,
    (person) => Number(person.control.monthByKey.get(month)?.hours || 0),
  ]));
  const statusRank = { insufficient: 0, watch: 1, unevaluated: 2, conform: 3 };
  return sortRows(rows, "collaborators", {
    name: (person) => person.name,
    department: (person) => person.department,
    total: (person) => person.control.totalHours,
    status: (person) => statusRank[person.control.globalStatus],
    ...monthGetters,
  });
}

function projectType(project) {
  return project?.projectType || (project?.internal ? "Interne" : "Client");
}

function filteredProjects() {
  const filters = state.columnFilters.projects;
  const globalFilters = state.projectGlobalFilters;
  const rows = state.data.projects
    .map((project) => {
      const current = aggregateEntityMonths(project);
      const linkedOrders = Number(project.orderAmount || 0);
      const globalHours = Number(project.hours || 0);
      const laborCost = Number(project.cost || 0);
      const fixedCosts = Number(project.fixedCosts || 0);
      const globalCost = Number(project.totalCost ?? (laborCost + fixedCosts));
      const netRevenue = Number(project.netRevenue || 0);
      const totalMargin = Number(project.totalMargin ?? (netRevenue - globalCost));
      const totalMarginRate = project.totalMarginRate ?? (netRevenue > 0 ? (totalMargin / netRevenue) * 100 : null);
      const odooStatus = project.active ? "En cours" : "Archivé";
      const type = projectType(project);
      const enriched = {
        ...project,
        current,
        linkedOrders,
        globalHours,
        laborCost,
        fixedCosts,
        globalCost,
        netRevenue,
        invoicedAmount: Number(project.invoiceAmount || 0),
        totalMargin,
        totalMarginRate,
        odooStatus,
        type,
      };
      enriched.status = projectStatus(enriched);
      return enriched;
    })
    .filter((project) => !globalFilters.type || project.type === globalFilters.type)
    .filter((project) => state.projectLifecycle === "all" || project.active === (state.projectLifecycle === "active"))
    .filter((project) => valueContains(project.name, filters.name))
    .filter((project) => valueContains(project.client, filters.client))
    .filter((project) => valueContains(project.type, filters.type))
    .filter((project) => valueContains(project.manager, filters.manager))
    .filter((project) => valueContains(oneDecimalFormatter.format(project.globalHours), filters.hours))
    .filter((project) => valueContains(numberFormatter.format(project.globalCost), filters.cost))
    .filter((project) => valueContains(numberFormatter.format(project.linkedOrders), filters.revenue))
    .filter((project) => valueContains(numberFormatter.format(project.invoicedAmount), filters.invoiced))
    .filter((project) => valueContains(numberFormatter.format(project.totalMargin), filters.margin));
  return sortRows(rows, "projects", {
    name: (project) => project.name,
    client: (project) => project.client,
    type: (project) => project.type,
    manager: (project) => project.manager,
    hours: (project) => project.globalHours,
    cost: (project) => project.globalCost,
    revenue: (project) => project.linkedOrders,
    invoiced: (project) => project.invoicedAmount,
    margin: (project) => project.totalMargin,
    odooStatus: (project) => project.odooStatus,
  });
}


function updateHeader() {
  const meta = viewMeta[state.view];
  document.querySelector("#view-eyebrow").textContent = meta.eyebrow;
  document.querySelector("#view-title").textContent = meta.title;
  const through = dateFormatter.format(new Date(`${state.data.meta.dataThrough}T12:00:00`));
  document.querySelector("#view-subtitle").textContent = `${meta.subtitle} · données au ${through}`;
}

function renderKpiCard({ label, value, meta, icon, color = "#f66729", badge = "" }) {
  return `
    <article class="kpi-card" style="--accent:${color}">
      <div class="kpi-head"><span>${esc(label)}</span><span class="kpi-icon">${icon}</span></div>
      <strong class="kpi-value">${esc(value)}</strong>
      <div class="kpi-meta">${badge ? `<span class="delta ${badge.startsWith("-") ? "negative" : ""}">${esc(badge)}</span>` : ""}<span>${esc(meta)}</span></div>
    </article>`;
}

function chartSeries() {
  const months = activeMonths();
  const cumulative = { cost: 0, laborCost: 0, otherCosts: 0, revenue: 0, orders: 0 };
  const cumulativeByBu = new Map();
  return months.map((month) => {
    state.data.monthlyBu
      .filter((row) => row.month === month && (state.bu === "all" || row.bu === state.bu))
      .forEach((row) => {
        const values = cumulativeByBu.get(row.bu) || { cost: 0, laborCost: 0, otherCosts: 0, revenue: 0, orders: 0 };
        values.cost += Number(row.totalCost ?? row.cost ?? 0);
        values.laborCost += Number(row.laborCost ?? row.cost ?? 0);
        values.otherCosts += Number(row.otherCosts || 0);
        values.revenue += Number(row.revenue || 0);
        values.orders += Number(row.orders || 0);
        cumulativeByBu.set(row.bu, values);
        cumulative.cost += Number(row.totalCost ?? row.cost ?? 0);
        cumulative.laborCost += Number(row.laborCost ?? row.cost ?? 0);
        cumulative.otherCosts += Number(row.otherCosts || 0);
        cumulative.revenue += Number(row.revenue || 0);
        cumulative.orders += Number(row.orders || 0);
      });
    return {
      month,
      ...cumulative,
      breakdown: [...cumulativeByBu.entries()]
        .map(([bu, values]) => ({ bu, ...values }))
        .filter((row) => row.orders || row.revenue || row.cost)
        .sort((left, right) => right.revenue - left.revenue),
    };
  });
}

function financialChart(series) {
  if (!series.length) return '<div class="empty-state"><p>Aucune donnée sur cette période.</p></div>';
  const width = 760;
  const height = 255;
  const pad = { left: 55, right: 18, top: 18, bottom: 38 };
  const chartWidth = width - pad.left - pad.right;
  const chartHeight = height - pad.top - pad.bottom;
  const maximum = Math.max(...series.flatMap((point) => [point.orders, point.revenue, point.cost]), 1) * 1.08;
  const x = (index) => pad.left + (series.length === 1 ? chartWidth / 2 : (index / (series.length - 1)) * chartWidth);
  const y = (value) => pad.top + chartHeight - (value / maximum) * chartHeight;
  const points = (key) => series.map((point, index) => `${x(index)},${y(point[key])}`).join(" ");
  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const gridY = pad.top + chartHeight * ratio;
    return `<line x1="${pad.left}" x2="${width - pad.right}" y1="${gridY}" y2="${gridY}" class="chart-grid-line" />
      <text x="${pad.left - 8}" y="${gridY + 3}" text-anchor="end" class="chart-label chart-y-label">${esc(compactCurrency(maximum * (1 - ratio)))}</text>`;
  }).join("");
  const labelStep = Math.max(1, Math.ceil(series.length / 8));
  const labels = series.map((point, index) => ({ point, index }))
    .filter(({ index }) => index === 0 || index === series.length - 1 || index % labelStep === 0)
    .map(({ point, index }) => `<text x="${x(index)}" y="${height - 10}" text-anchor="middle" class="chart-label">${esc(formatMonthLabel(point.month))}</text>`).join("");
  const monthlyOverviews = series.map((point, index) => {
    const zoneWidth = series.length > 1 ? chartWidth / (series.length - 1) : chartWidth;
    return `<g class="financial-point-overview">
      <circle cx="${x(index)}" cy="${y(point.orders)}" r="3.5" class="chart-point chart-point-orders" />
      <circle cx="${x(index)}" cy="${y(point.revenue)}" r="3.5" class="chart-point chart-point-revenue" />
      <circle cx="${x(index)}" cy="${y(point.cost)}" r="3.5" class="chart-point chart-point-financial-cost" />
      <rect x="${Math.max(pad.left, x(index) - zoneWidth / 2)}" y="${pad.top}" width="${Math.min(zoneWidth, width - pad.right - Math.max(pad.left, x(index) - zoneWidth / 2))}" height="${chartHeight}" class="chart-hover-zone" data-financial-point="${index}" tabindex="0" aria-label="Détail de ${esc(formatMonthLabel(point.month))} ${esc(point.month.slice(0, 4))}"></rect>
    </g>`;
  }).join("");
  return `<svg class="line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Évolution mensuelle des commandes, de la facturation nette et des coûts">
    ${grid}
    <polyline points="${points("orders")}" class="chart-line-orders" />
    <polyline points="${points("revenue")}" class="chart-line-financial-revenue" />
    <polyline points="${points("cost")}" class="chart-line-financial-cost" />
    ${monthlyOverviews}
    ${labels}
  </svg>`;
}

function financialTooltipTable(point) {
  const rows = (point.breakdown || []).filter((row) =>
    [row.orders, row.revenue, row.cost].some((value) => Math.abs(Number(value || 0)) > 0.005),
  );
  const valueCell = (value) => Math.abs(Number(value || 0)) > 0.005 ? esc(compactCurrency(value)) : "—";
  return `<div class="financial-tooltip-heading"><div><strong>${esc(formatMonthLabel(point.month))} ${esc(point.month.slice(0, 4))}</strong><span>Cumul depuis le début de l’exercice</span></div><button type="button" class="financial-tooltip-close" aria-label="Fermer">×</button></div>
    <div class="financial-tooltip-totals"><span>Commandes <strong>${esc(compactCurrency(point.orders))}</strong></span><span>CA net <strong>${esc(compactCurrency(point.revenue))}</strong></span><span>Coûts <strong>${esc(compactCurrency(point.cost))}</strong></span></div>
    <table><thead><tr><th>Business unit</th><th>Commandes</th><th>CA net</th><th>Coûts</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td>${esc(row.bu)}</td><td>${valueCell(row.orders)}</td><td>${valueCell(row.revenue)}</td><td>${valueCell(row.cost)}</td></tr>`).join("")}
    </tbody></table>`;
}

function bindFinancialChartTooltips(series) {
  const tooltip = document.querySelector("#financial-chart-tooltip");
  if (!tooltip) return;
  const place = (event) => {
    const gap = 14;
    const width = tooltip.offsetWidth;
    const height = tooltip.offsetHeight;
    const pointerX = Number.isFinite(event.clientX) ? event.clientX : window.innerWidth / 2;
    const pointerY = Number.isFinite(event.clientY) ? event.clientY : window.innerHeight / 2;
    const left = Math.min(window.innerWidth - width - gap, Math.max(gap, pointerX + gap));
    const top = Math.min(window.innerHeight - height - gap, Math.max(gap, pointerY + gap));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  };
  const hide = () => {
    tooltip.hidden = true;
    delete tooltip.dataset.activePoint;
  };
  tooltip.addEventListener("click", (event) => event.stopPropagation());
  document.querySelectorAll("[data-financial-point]").forEach((zone) => {
    const toggle = (event) => {
      event.stopPropagation();
      const index = Number(zone.dataset.financialPoint);
      if (!tooltip.hidden && tooltip.dataset.activePoint === String(index)) {
        hide();
        return;
      }
      const point = series[index];
      if (!point) return;
      tooltip.innerHTML = financialTooltipTable(point);
      tooltip.hidden = false;
      tooltip.dataset.activePoint = String(index);
      place(event);
      tooltip.querySelector(".financial-tooltip-close")?.addEventListener("click", (closeEvent) => {
        closeEvent.stopPropagation();
        hide();
      });
      document.addEventListener("click", hide, { once: true });
      document.addEventListener("keydown", (keyEvent) => {
        if (keyEvent.key === "Escape") hide();
      }, { once: true });
    };
    zone.addEventListener("click", toggle);
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") toggle(event);
    });
  });
}

function invoiceStatusMetrics() {
  const months = new Set(activeMonths());
  const groups = new Map();
  const statusPresentation = {
    paid: { label: "Payées", tone: "paid" },
    due: { label: "En cours de paiement", tone: "due" },
    late: { label: "En retard", tone: "late" },
    cancelled: { label: "Annulées", tone: "neutral" },
    canceled: { label: "Annulées", tone: "neutral" },
    draft: { label: "Brouillons", tone: "neutral" },
  };
  (state.data.finance?.invoiceStatuses || [])
    .filter((row) => months.has(row.month) && (state.bu === "all" || row.bu === state.bu))
    .forEach((row) => {
      const key = normalized(row.status || "Non renseigné");
      const presentation = statusPresentation[key] || { label: row.status || "Non renseigné", tone: "neutral" };
      const current = groups.get(key) || { ...presentation, count: 0, amount: 0 };
      current.count += Number(row.count || 0);
      current.amount += Number(row.amount || 0);
      groups.set(key, current);
    });
  return [...groups.values()].sort((a, b) => b.amount - a.amount);
}

function lineChart(series, { showYears = false, compactYAxis = false } = {}) {
  if (!series.length) return '<div class="empty-state"><p>Aucune donnée sur cette période.</p></div>';
  const width = 680;
  const height = 245;
  const pad = { left: 48, right: 16, top: 20, bottom: showYears ? 42 : 32 };
  const chartWidth = width - pad.left - pad.right;
  const chartHeight = height - pad.top - pad.bottom;
  const maximum = Math.max(...series.flatMap((point) => [point.cost, point.revenue]), 1) * 1.08;
  const x = (index) => pad.left + (series.length === 1 ? chartWidth / 2 : (index / (series.length - 1)) * chartWidth);
  const y = (value) => pad.top + chartHeight - (value / maximum) * chartHeight;
  const revenuePoints = series.map((point, index) => `${x(index)},${y(point.revenue)}`).join(" ");
  const costPoints = series.map((point, index) => `${x(index)},${y(point.cost)}`).join(" ");
  const areaPoints = `${pad.left},${pad.top + chartHeight} ${revenuePoints} ${x(series.length - 1)},${pad.top + chartHeight}`;
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const gridY = pad.top + chartHeight * ratio;
      const labelValue = maximum * (1 - ratio);
      return `<line x1="${pad.left}" x2="${width - pad.right}" y1="${gridY}" y2="${gridY}" class="chart-grid-line" />
        <text x="${pad.left - 8}" y="${gridY + 3}" text-anchor="end" class="chart-label${compactYAxis ? " chart-y-label" : ""}">${esc(compactCurrency(labelValue))}</text>`;
    })
    .join("");
  const labelStep = Math.max(1, Math.ceil(series.length / 8));
  const labels = series
    .map((point, index) => ({ point, index }))
    .filter(({ index }) => index === 0 || index === series.length - 1 || index % labelStep === 0)
    .map(
      ({ point, index }) => showYears
        ? `<text x="${x(index)}" y="${height - 20}" text-anchor="middle" class="chart-label"><tspan x="${x(index)}">${esc(formatMonthLabel(point.month))}</tspan><tspan x="${x(index)}" dy="11" class="chart-year-label">${esc(point.month.slice(0, 4))}</tspan></text>`
        : `<text x="${x(index)}" y="${height - 8}" text-anchor="middle" class="chart-label">${esc(formatMonthLabel(point.month))}</text>`,
    )
    .join("");
  return `<svg class="line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Évolution mensuelle du chiffre d’affaires et des coûts">
    <defs><linearGradient id="area-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f66729"/><stop offset="1" stop-color="#f66729" stop-opacity="0"/></linearGradient></defs>
    ${grid}
    <polygon points="${areaPoints}" fill="url(#area-fill)" class="chart-area" />
    <polyline points="${revenuePoints}" class="chart-line-revenue" />
    <polyline points="${costPoints}" class="chart-line-cost" />
    ${series.map((point, index) => `<circle cx="${x(index)}" cy="${y(point.revenue)}" r="3" fill="#fff" stroke="#f66729" stroke-width="2"/>`).join("")}
    ${labels}
  </svg>`;
}

function overviewMetrics() {
  const totals = aggregateMonths(state.data.monthlyBu);
  const projects = filteredProjects();
  const collaborators = filteredCollaborators();
  const margin = totals.revenue - totals.totalCost;
  const marginRate = totals.revenue ? (margin / totals.revenue) * 100 : 0;
  const internalHours = projects
    .filter(
      (project) =>
        normalized(project.name).startsWith("interne") ||
        normalized(project.client).startsWith("mandarine"),
    )
    .reduce((sum, project) => sum + project.current.hours, 0);
  const internalShare = totals.hours ? (internalHours / totals.hours) * 100 : 0;
  return { totals, projects, collaborators, margin, marginRate, internalHours, internalShare, remainingToInvoice: totals.orders - totals.revenue };
}

function renderOverview() {
  const metrics = overviewMetrics();
  const financialSeries = chartSeries();
  const critical = state.data.alerts.find((alert) => alert.level === "critical") || state.data.alerts[0];
  const buTotals = [...new Set(state.data.monthlyBu.map((row) => row.bu))]
    .map((bu) => ({ bu, ...aggregateMonths(state.data.monthlyBu, { bu }) }))
    .filter((row) => row.orders || row.revenue || row.totalCost)
    .sort((a, b) => b.revenue - a.revenue);
  const invoiceStatuses = invoiceStatusMetrics();

  appView.innerHTML = `
    ${critical ? `<div class="alert-banner"><span class="alert-symbol">!</span><p><strong>${esc(critical.title)}</strong> ${esc(critical.detail)}</p><button class="text-button" data-go-view="${esc(critical.target)}">${esc(critical.action)} →</button></div>` : ""}
    <section class="kpi-grid overview-kpi-grid">
      ${renderKpiCard({ label: "Commandes HT", value: compactCurrency(metrics.totals.orders), meta: "Commandes Sellsy rapprochées", icon: "↗", color: "#21865d" })}
      ${renderKpiCard({ label: "CA net facturé HT", value: compactCurrency(metrics.totals.revenue), meta: `${compactCurrency(metrics.totals.invoiceAmount)} facturé · ${compactCurrency(metrics.totals.creditAmount)} d’avoirs`, icon: "€", color: "#5671d8" })}
      ${renderKpiCard({ label: "Reste à facturer HT", value: compactCurrency(metrics.remainingToInvoice), meta: "Commandé moins CA net facturé", icon: "→", color: "#e6a126" })}
      ${renderKpiCard({ label: "Coût main-d’œuvre HT", value: compactCurrency(metrics.totals.laborCost), meta: "Temps valorisés dans Odoo", icon: "◷", color: "#1d8d82" })}
      ${renderKpiCard({ label: "Autres coûts HT", value: compactCurrency(metrics.totals.otherCosts), meta: "Écritures analytiques à date", icon: "◆", color: "#9656c7" })}
      ${renderKpiCard({ label: "Marge opérationnelle", value: compactCurrency(metrics.margin), meta: `${formatPercent(metrics.marginRate)} du CA net`, icon: "≈", color: metrics.margin >= 0 ? "#21865d" : "#c94343", badge: metrics.margin >= 0 ? "positive" : "-négative" })}
    </section>

    <section class="dashboard-grid">
      <article class="panel">
        <header class="panel-header">
          <div class="panel-title"><h2>Commandes, facturation nette et coûts cumulés</h2><p>Cumul depuis le début de l’exercice · survolez un mois pour la répartition par BU</p></div>
          <div class="legend"><span style="color:#21865d"><i></i>Commandes</span><span style="color:#5671d8"><i></i>Facturation nette</span><span class="dashed" style="color:#1d8d82"><i></i>Coûts</span></div>
        </header>
        <div class="chart-wrap">${financialChart(financialSeries)}<div id="financial-chart-tooltip" class="financial-chart-tooltip" hidden></div></div>
      </article>
      <article class="panel">
        <header class="panel-header"><div class="panel-title"><h2>Situation actuelle des factures</h2><p>Statut Sellsy actuel des factures émises sur la période</p></div></header>
        <div class="invoice-status-list">
          ${invoiceStatuses.length ? invoiceStatuses.map((row) => `<div class="invoice-status-row"><span class="status-pill invoice-status-${esc(row.tone)}">${esc(row.label)}</span><strong>${esc(compactCurrency(row.amount))}</strong><small>${numberFormatter.format(row.count)} facture(s)</small></div>`).join("") : '<div class="empty-state"><p>Aucun statut de facture disponible.</p></div>'}
        </div>
      </article>
    </section>

    <section class="dashboard-grid equal">
      <article class="panel">
        <header class="panel-header"><div class="panel-title"><h2>Pilotage par business unit</h2><p>Comparaison macro sur l’exercice sélectionné</p></div><button class="text-button" data-go-view="projects">Détail →</button></header>
        <div class="finance-table-wrap"><table class="finance-table"><thead><tr><th>BU</th><th>Commandes</th><th>CA net</th><th>Reste</th><th>Coûts</th><th>Marge</th></tr></thead><tbody>
          ${buTotals.map((row) => `<tr><td>${esc(row.bu)}</td><td>${esc(compactCurrency(row.orders))}</td><td>${esc(compactCurrency(row.revenue))}</td><td>${esc(compactCurrency(row.orders - row.revenue))}</td><td>${esc(compactCurrency(row.totalCost))}</td><td class="${row.revenue - row.totalCost < 0 ? "negative-value" : "positive-value"}">${esc(compactCurrency(row.revenue - row.totalCost))}</td></tr>`).join("")}
        </tbody></table></div>
      </article>
      <article class="panel">
        <header class="panel-header"><div class="panel-title"><h2>Points de vigilance</h2><p>Contrôles prioritaires issus des rapprochements</p></div></header>
        <div class="alerts-list">
          ${state.data.alerts.map((alert) => `<div class="alert-item"><span class="alert-level ${alert.level === "critical" ? "critical" : ""}">${alert.level === "critical" ? "!" : "△"}</span><div class="alert-content"><strong>${esc(alert.title)}</strong><span>${esc(alert.detail)}</span></div><button class="text-button" data-go-view="${esc(alert.target)}">Voir</button></div>`).join("")}
        </div>
      </article>
    </section>`;
  bindFinancialChartTooltips(financialSeries);
}

function initials(name) {
  return String(name)
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function pagination(total, pageSize) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  state.page = Math.min(state.page, pages);
  const visible = new Set([1, pages]);
  for (let page = Math.max(1, state.page - 2); page <= Math.min(pages, state.page + 2); page += 1) visible.add(page);
  const pageNumbers = [...visible].sort((left, right) => left - right);
  const buttons = [];
  buttons.push(`<button type="button" data-page="${Math.max(1, state.page - 1)}" ${state.page === 1 ? "disabled" : ""} aria-label="Page précédente">‹</button>`);
  pageNumbers.forEach((page, index) => {
    if (index && page - pageNumbers[index - 1] > 1) buttons.push('<span class="pagination-gap">…</span>');
    buttons.push(`<button type="button" data-page="${page}" class="${page === state.page ? "is-current" : ""}">${page}</button>`);
  });
  buttons.push(`<button type="button" data-page="${Math.min(pages, state.page + 1)}" ${state.page === pages ? "disabled" : ""} aria-label="Page suivante">›</button>`);
  return { pages, html: `<nav class="pagination" aria-label="Pagination">${buttons.join("")}</nav>` };
}


function projectDetailPagination(items, key, pageSize = 5) {
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const current = Math.min(state.projectDetailPages[key] || 1, pages);
  state.projectDetailPages[key] = current;
  const visible = new Set([1, pages]);
  for (let page = Math.max(1, current - 1); page <= Math.min(pages, current + 1); page += 1) visible.add(page);
  const pageNumbers = [...visible].sort((left, right) => left - right);
  const buttons = [`<button type="button" data-project-detail-key="${key}" data-project-detail-page="${Math.max(1, current - 1)}" ${current === 1 ? "disabled" : ""} aria-label="Page précédente">‹</button>`];
  pageNumbers.forEach((page, index) => {
    if (index && page - pageNumbers[index - 1] > 1) buttons.push('<span class="pagination-gap">…</span>');
    buttons.push(`<button type="button" data-project-detail-key="${key}" data-project-detail-page="${page}" class="${page === current ? "is-current" : ""}">${page}</button>`);
  });
  buttons.push(`<button type="button" data-project-detail-key="${key}" data-project-detail-page="${Math.min(pages, current + 1)}" ${current === pages ? "disabled" : ""} aria-label="Page suivante">›</button>`);
  const startIndex = (current - 1) * pageSize;
  return {
    rows: items.slice(startIndex, startIndex + pageSize),
    start: items.length ? startIndex + 1 : 0,
    end: Math.min(startIndex + pageSize, items.length),
    html: `<nav class="pagination" aria-label="Pagination">${buttons.join("")}</nav>`,
  };
}

function monthControlTooltip(month) {
  const presentation = timeControlPresentation[month.status] || timeControlPresentation.unevaluated;
  const lines = [`Mois : ${formatMonthLabel(month.month, "long")}`, `Heures saisies : ${formatHours(month.hours)}`];
  if (month.expectedHours === null || month.expectedHours === undefined) {
    lines.push(`Capacité attendue : non évaluée`, `Statut : ${presentation.label}`);
  } else {
    lines.push(
      `Capacité attendue : ${formatHours(month.expectedHours)}`,
      `Taux de remplissage : ${formatPercent(month.coverageRate)}`,
      `Écart : ${numberFormatter.format(Number(month.hours || 0) - Number(month.expectedHours || 0))} h`,
      `Statut : ${presentation.label}`,
    );
  }
  lines.push(
    `Client : ${formatHours(month.clientHours)}`,
    `Interne : ${formatHours(month.internalHours)}`,
    `Absences : ${formatHours(month.absenceHours)}`,
  );
  if (Number(month.unclassifiedHours || 0)) lines.push(`Non classé : ${formatHours(month.unclassifiedHours)}`);
  return lines.join("\n");
}

function renderMonthControlCell(month) {
  const presentation = timeControlPresentation[month.status] || timeControlPresentation.unevaluated;
  const evaluated = month.coverageRate !== null && month.coverageRate !== undefined;
  const progress = evaluated ? Math.max(0, Math.min(Number(month.coverageRate || 0), 100)) : 0;
  return `<td class="control-month-cell control-month-${esc(month.status)}" title="${esc(monthControlTooltip(month))}">
    <strong>${evaluated || Number(month.hours || 0) ? esc(formatHours(month.hours)) : "—"}</strong>
    <span class="control-month-track" aria-hidden="true"><span style="width:${progress}%"></span></span>
  </td>`;
}

function globalControlTooltip(person) {
  const counts = person.control.counts;
  return [
    `Mois conformes : ${counts.conform}`,
    `Mois à vérifier : ${counts.watch}`,
    `Mois insuffisants : ${counts.insufficient}`,
    `Mois non évalués : ${counts.unevaluated}`,
    `Heures manquantes estimées : ${formatHours(person.control.missingHours)}`,
  ].join("\n");
}

function renderCollaborators() {
  const people = filteredCollaborators();
  const monthKeys = collaboratorPeriodMonths();
  const selectedPeriod = selectedFiscalPeriod();
  const totalHours = people.reduce((sum, person) => sum + person.control.totalHours, 0);
  const totalCost = people.reduce((sum, person) => sum + person.current.cost, 0);
  const average = people.length ? totalHours / people.length : 0;
  const departmentCount = new Set(people.map((person) => person.department)).size;
  const pageSize = 10;
  const pager = pagination(people.length, pageSize);
  const rows = people.slice((state.page - 1) * pageSize, state.page * pageSize);
  const blankMonthFilters = monthKeys.map(() => '<th aria-hidden="true"></th>').join("");

  appView.innerHTML = `
    <section class="kpi-grid">
      ${renderKpiCard({ label: "Collaborateurs suivis", value: numberFormatter.format(people.length), meta: `${departmentCount} métiers représentés`, icon: "◎", color: "#f66729" })}
      ${renderKpiCard({ label: "Temps saisi", value: formatHours(totalHours), meta: "Somme des 12 mois de l’exercice", icon: "◷", color: "#5671d8" })}
      ${renderKpiCard({ label: "Moyenne par collaborateur", value: formatHours(average), meta: "Sur l’exercice sélectionné", icon: "∿", color: "#21865d" })}
      ${renderKpiCard({ label: "Coût direct du temps", value: compactCurrency(totalCost), meta: "Hors coûts d’acquisition et externes", icon: "€", color: "#a064c7" })}
    </section>
    <section class="panel table-panel collaborator-control-panel">
      <div class="table-toolbar collaborator-control-toolbar">
        <div>
          <h2>Suivi individuel — ${esc(selectedPeriod?.label || state.data.meta.periodLabel)}</h2>
          <p>Contrôle mensuel indicatif sur une base globale de 7 h 40 par jour ouvré. Les absences saisies comptent comme temps rempli.</p>
          <div class="control-legend" aria-label="Légende des statuts">
            <span class="pill">À jour</span><span class="pill warning">À vérifier</span><span class="pill danger">En retard</span><span class="pill archived">Non évalué</span>
          </div>
        </div>
        <div class="collaborator-control-actions">
          <button type="button" class="control-filter-button ${state.collaboratorInsufficientOnly ? "is-active" : ""}" data-collaborator-insufficient aria-pressed="${state.collaboratorInsufficientOnly}">Au moins un mois insuffisant</button>
          <span class="table-count">${people.length} résultats</span>
        </div>
      </div>
      <div class="table-scroll collaborator-control-scroll"><table class="collaborator-control-table">
        <thead>
          <tr>
            <th class="control-sticky-name">${sortableHeader("collaborators", "name", "Collaborateur")}</th>
            <th class="control-sticky-job">${sortableHeader("collaborators", "department", "Métier")}</th>
            ${monthKeys.map((month) => `<th class="control-month-heading" title="${esc(formatMonthLabel(month, "long"))}">${sortableHeader("collaborators", `month:${month}`, formatMonthLabel(month, "short"))}</th>`).join("")}
            <th class="control-total-heading">${sortableHeader("collaborators", "total", "Total")}</th>
            <th class="control-reading-heading">${sortableHeader("collaborators", "status", "Lecture")}</th>
          </tr>
          <tr class="column-filter-row">
            <th class="control-sticky-name">${columnFilter("collaborators", "name", "Collaborateur")}</th>
            <th class="control-sticky-job">${columnFilter("collaborators", "department", "Métier")}</th>
            ${blankMonthFilters}
            <th class="control-total-heading">${columnFilter("collaborators", "total", "Total")}</th>
            <th class="control-reading-heading">${columnFilter("collaborators", "status", "Lecture")}</th>
          </tr>
        </thead>
        <tbody>${rows.map((person) => `<tr>
          <td class="control-sticky-name"><div class="entity-cell"><span class="entity-icon">${esc(initials(person.name))}</span><div class="entity-label"><strong>${esc(person.name)}</strong><span>${person.active ? "Actif" : "Inactif"}</span></div></div></td>
          <td class="control-sticky-job">${esc(person.department)}</td>
          ${person.control.months.map(renderMonthControlCell).join("")}
          <td class="control-total-cell"><strong>${esc(formatHours(person.control.totalHours))}</strong></td>
          <td class="control-reading-cell" title="${esc(globalControlTooltip(person))}"><span class="pill ${person.status[1]}">${esc(person.status[0])}</span></td>
        </tr>`).join("")}</tbody>
      </table></div>
      <footer class="table-footer"><span>${people.length ? (state.page - 1) * pageSize + 1 : 0}–${Math.min(state.page * pageSize, people.length)} sur ${people.length}</span>${pager.html}</footer>
    </section>`;
}

function projectStatus(project) {
  if (project.internal || project.type === "Interne") return ["Interne", ""];
  if (Number(project.netRevenue || 0) > 0 && Number(project.totalMargin || 0) < 0) return ["Marge négative", "danger"];
  if (Number(project.invoiceAmount || 0) === 0 && Number(project.globalCost || 0) > 0) return ["Facturation à rapprocher", "warning"];
  return ["Sous contrôle", ""];
}

function renderProjects() {
  const projects = filteredProjects();
  const totalCost = projects.reduce((sum, project) => sum + project.laborCost, 0);
  const totalFixedCosts = projects.reduce((sum, project) => sum + project.fixedCosts, 0);
  const fixedCostCategories = projects.reduce((totals, project) => {
    Object.entries(project.fixedCostCategories || {}).forEach(([code, value]) => {
      totals[code] = (totals[code] || 0) + Number(value || 0);
    });
    return totals;
  }, {});
  const fixedCostBreakdown = [
    `Acquisition ${compactCurrency(fixedCostCategories["COUT-ACQ"] || 0)}`,
    `Internes ${compactCurrency(fixedCostCategories["COUT-INT"] || 0)}`,
    `Licences ${compactCurrency(fixedCostCategories["COUT-LIC"] || 0)}`,
  ].join(" · ");
  const linkedOrders = projects.reduce((sum, project) => sum + project.linkedOrders, 0);
  const clientProjects = projects.filter((project) => project.type === "Client");
  const atRisk = projects.filter((project) => projectStatus(project)[1] === "danger").length;
  const pageSize = 10;
  const pager = pagination(projects.length, pageSize);
  const rows = projects.slice((state.page - 1) * pageSize, state.page * pageSize);
  const projectGlobalFilterChips = Object.entries(state.projectGlobalFilters)
    .filter(([, value]) => value)
    .map(([key, value]) => `<button type="button" class="filter-chip" data-remove-project-filter="${key}" aria-label="Supprimer le filtre Type de projet ${esc(value)}"><span class="filter-chip-label">Type de projet</span><span class="filter-chip-value">${esc(value)}</span><span class="filter-chip-remove" aria-hidden="true">×</span></button>`)
    .join("");

  appView.innerHTML = `
    <section class="kpi-grid project-kpi-grid">
      ${renderKpiCard({ label: state.projectLifecycle === "active" ? "Projets en cours" : "Projets affichés", value: numberFormatter.format(projects.length), meta: `${clientProjects.length} projets hors interne`, icon: "▦", color: "#f66729" })}
      ${renderKpiCard({ label: "Coût MO cumulé", value: compactCurrency(totalCost), meta: "Toutes périodes Odoo", icon: "€", color: "#5671d8" })}
      ${renderKpiCard({ label: "Coûts fixes cumulés", value: compactCurrency(totalFixedCosts), meta: fixedCostBreakdown, icon: "◆", color: "#a064c7" })}
      ${renderKpiCard({ label: "Commandes rapprochées", value: compactCurrency(linkedOrders), meta: "Montant HT Sellsy rattaché", icon: "↗", color: "#21865d" })}
      ${renderKpiCard({ label: "Marges négatives", value: numberFormatter.format(atRisk), meta: "Après coûts MO et coûts fixes", icon: "!", color: atRisk ? "#c94343" : "#21865d" })}
    </section>
    <section class="panel table-panel">
      <div class="table-toolbar project-toolbar"><div><h2>Portefeuille projets</h2><p>Rentabilité cumulée toutes périodes, consolidée par les relations techniques Odoo.</p><div class="project-filter-bar" aria-label="Filtres globaux du tableau"><span class="project-filter-label">Filtres actifs</span><div class="active-filter-chips">${projectGlobalFilterChips || '<span class="project-filter-empty">Aucun filtre global</span>'}</div></div></div><span class="table-count">${projects.length} résultats</span></div>
      <div class="table-scroll"><table>
        <thead>
          <tr><th>${sortableHeader("projects", "name", "Projet")}</th><th>${sortableHeader("projects", "client", "Client final")}</th><th>${sortableHeader("projects", "type", "Type de projet")}</th><th>${sortableHeader("projects", "manager", "Responsable")}</th><th>${sortableHeader("projects", "hours", "Heures cumulées")}</th><th>${sortableHeader("projects", "cost", "Coût global à date")}</th><th>${sortableHeader("projects", "revenue", "Commandé HT")}</th><th>${sortableHeader("projects", "invoiced", "Facturé HT")}</th><th>${sortableHeader("projects", "margin", "Marge sur facturé")}</th><th>${sortableHeader("projects", "odooStatus", "État Odoo")}</th></tr>
          <tr class="column-filter-row"><th>${columnFilter("projects", "name", "Projet")}</th><th>${columnFilter("projects", "client", "Client")}</th><th>${columnFilter("projects", "type", "Type de projet")}</th><th>${columnFilter("projects", "manager", "Responsable")}</th><th>${columnFilter("projects", "hours", "Heures")}</th><th>${columnFilter("projects", "cost", "Coût à date")}</th><th>${columnFilter("projects", "revenue", "Commandé")}</th><th>${columnFilter("projects", "invoiced", "Facturé")}</th><th>${columnFilter("projects", "margin", "Marge")}</th><th><select class="column-filter project-state-filter" data-project-lifecycle aria-label="Filtrer l'état Odoo"><option value="active" ${state.projectLifecycle === "active" ? "selected" : ""}>En cours</option><option value="archived" ${state.projectLifecycle === "archived" ? "selected" : ""}>Archivés</option><option value="all" ${state.projectLifecycle === "all" ? "selected" : ""}>Tous</option></select></th></tr>
        </thead>
        <tbody>${rows.map((project) => {
          return `<tr class="clickable-row" data-project-id="${esc(project.id)}" tabindex="0" aria-label="Ouvrir la fiche du projet ${esc(project.name)}"><td><div class="entity-cell"><span class="entity-icon">${esc(initials(project.name))}</span><div class="entity-label"><strong title="${esc(project.name)}">${esc(project.name)}</strong></div></div></td><td>${esc(project.client)}</td><td>${esc(project.type)}</td><td>${esc(project.manager)}</td><td>${esc(formatHours(project.globalHours))}</td><td><strong title="Main-d'œuvre ${esc(formatCurrency(project.laborCost))} · Coûts fixes échus ${esc(formatCurrency(project.fixedCosts))}">${esc(formatCurrency(project.globalCost))}</strong></td><td>${esc(formatCurrency(project.linkedOrders))}</td><td>${esc(formatCurrency(project.invoicedAmount))}</td><td style="color:${project.totalMargin < 0 ? "#c94343" : "#21865d"}">${project.netRevenue > 0 ? `${esc(formatCurrency(project.totalMargin))} (${esc(formatPercent(project.totalMarginRate))})` : "—"}</td><td><span class="pill ${project.active ? "" : "archived"}">${esc(project.odooStatus)}</span></td></tr>`;
        }).join("")}</tbody>
      </table></div>
      <footer class="table-footer"><span>${projects.length ? (state.page - 1) * pageSize + 1 : 0}–${Math.min(state.page * pageSize, projects.length)} sur ${projects.length}</span>${pager.html}</footer>
    </section>`;
}

function projectCashFlowChart(project) {
  const rows = project.months || [];
  const eventMonths = rows.map((row) => row.month).filter(Boolean).sort();
  if (!eventMonths.length) return '<div class="empty-state"><p>Aucune donnée datée pour ce projet.</p></div>';
  const values = new Map(rows.map((row) => [row.month, row]));
  const start = new Date(`${eventMonths[0]}-01T00:00:00Z`);
  const end = new Date(`${eventMonths[eventMonths.length - 1]}-01T00:00:00Z`);
  const series = [];
  let cumulativeCost = 0;
  let cumulativeRevenue = 0;
  for (const cursor = new Date(start); cursor <= end; cursor.setUTCMonth(cursor.getUTCMonth() + 1)) {
    const month = `${cursor.getUTCFullYear()}-${String(cursor.getUTCMonth() + 1).padStart(2, "0")}`;
    const row = values.get(month) || {};
    cumulativeCost += Number(row.cost || 0) + Number(row.fixedCosts || 0);
    cumulativeRevenue += Number(row.invoiced || 0) - Number(row.credits || 0);
    series.push({ month, cost: cumulativeCost, revenue: cumulativeRevenue });
  }
  const balance = cumulativeRevenue - cumulativeCost;
  return `<div class="cash-flow-summary"><span><i class="cash-flow-invoiced"></i>Facturation nette cumulée</span><span><i class="cash-flow-cost"></i>Coûts cumulés</span><strong class="${balance < 0 ? "negative" : ""}">Solde : ${esc(formatCurrency(balance))}</strong></div>
    <div class="chart-wrap">${lineChart(series, { showYears: true, compactYAxis: true })}</div>
    <p class="chart-period">Du ${esc(formatMonthLabel(eventMonths[0], "long"))} au ${esc(formatMonthLabel(eventMonths[eventMonths.length - 1], "long"))}</p>`;
}

function renderProjectDetail() {
  const project = state.data.projects.find((item) => String(item.id) === String(state.selectedProjectId));
  if (!project) {
    state.selectedProjectId = null;
    renderProjects();
    return;
  }
  const ordered = Number(project.orderAmount || 0);
  const invoiced = Number(project.invoiceAmount || 0);
  const credited = Number(project.creditAmount || 0);
  const netRevenue = Number(project.netRevenue || 0);
  const cumulativeCost = Number(project.cost || 0);
  const otherCosts = Number(project.fixedCosts || 0);
  const totalCost = Number(project.totalCost ?? (cumulativeCost + otherCosts));
  const globalHours = Number(project.hours || 0);
  const consumption = netRevenue > 0 ? (totalCost / netRevenue) * 100 : 0;
  const taskRows = (project.tasks || []).map((task) => {
    const months = new Set(activeMonths());
    const periodHours = (task.months || []).filter((row) => months.has(row.month)).reduce((sum, row) => sum + Number(row.hours || 0), 0);
    return { ...task, periodHours };
  });
  const departmentRows = (project.departmentCosts || []).map((department) => {
    const period = {
      hours: Number(department.hours || 0),
      cost: Number(department.cost || 0),
    };
    return { ...department, period };
  }).filter((department) => department.period.hours || department.period.cost).sort((left, right) => right.period.cost - left.period.cost);
  const maxDepartmentCost = Math.max(...departmentRows.map((department) => department.period.cost), 1);
  const orderItems = project.orders || [];
  const documentItems = (project.invoices || []).map((item) => ({ ...item, documentType: "Facture" }))
    .concat((project.creditNotes || []).map((item) => ({ ...item, documentType: "Avoir" })));
  const orderPager = projectDetailPagination(orderItems, "orders");
  const documentPager = projectDetailPagination(documentItems, "documents");
  const sellsyOrderStatus = {
    invoiced: { label: "Facturée", className: "" },
    partialinvoiced: { label: "Partiellement facturée", className: "warning" },
    accepted: { label: "Acceptée", className: "" },
    read: { label: "Lue", className: "archived" },
    draft: { label: "Brouillon", className: "archived" },
    cancelled: { label: "Annulée", className: "danger" },
  };
  const sellsyDocumentStatus = {
    paid: { label: "Payée", className: "" },
    due: { label: "À payer", className: "warning" },
    late: { label: "En retard", className: "danger" },
    spent: { label: "Utilisé", className: "" },
    cancelled: { label: "Annulé", className: "danger" },
  };
  const taskStatusDefinitions = [
    { value: "NORMAL", label: "Normal", className: "" },
    { value: "WARNING", label: "À surveiller", className: "warning" },
    { value: "OVERRUN", label: "Dépassement", className: "danger" },
    { value: "NO_BUDGET", label: "Budget absent", className: "warning" },
  ];
  const taskStatusCounts = Object.fromEntries(taskStatusDefinitions.map(({ value }) => [
    value,
    taskRows.filter((task) => task.status === value).length,
  ]));
  const filteredTaskRows = state.projectTaskStatus
    ? taskRows.filter((task) => task.status === state.projectTaskStatus)
    : taskRows;
  const taskPager = projectDetailPagination(filteredTaskRows, "tasks", 10);

  appView.innerHTML = `
    <div class="detail-heading"><button type="button" class="secondary-button" data-back-projects>← Portefeuille projets</button><div><p class="eyebrow">${esc(project.client)}</p><h2>${esc(project.name)}</h2><p>${esc(project.manager)} · ${esc(project.bu)}</p></div></div>
    ${project.odooCorrectionRequired ? `<div class="alert-banner ${project.fictitiousReplacementExists ? "danger" : ""}"><span class="alert-symbol">!</span><p><strong>Correction à effectuer dans Odoo.</strong> ${esc(formatHours(project.fictitiousTimeHours))} et ${esc(formatCurrency(project.fictitiousTimeCost))} saisis via ${esc((project.fictitiousEmployees || []).join(", ") || "des collaborateurs fictifs")} sont provisoirement réintégrés aux indicateurs.${project.fictitiousReplacementExists ? " Des coûts analytiques coexistent sur ce projet : vérifiez le risque de double comptage lors de la régularisation." : ""}</p></div>` : ""}
    <section class="panel profitability-panel">
      <header class="panel-header"><div class="panel-title"><h2>Rapport de rentabilité</h2><p>Les commandes, factures, avoirs, coûts et temps sont cumulés sur toute la durée du projet.</p></div><span class="pill ${consumption > 100 ? "danger" : consumption > 85 ? "warning" : ""}">${netRevenue > 0 ? `${esc(formatPercent(consumption))} du CA net consommé` : "CA net non rapproché"}</span></header>
      <div class="profitability-track"><span style="width:${Math.min(consumption, 100)}%;background:${consumption > 100 ? "#c94343" : consumption > 85 ? "#e39a2f" : "#21865d"}"></span></div>
      <div class="kpi-grid project-kpi-grid project-profitability-kpis">
        ${renderKpiCard({ label: "Commandé HT", value: compactCurrency(ordered), meta: `${numberFormatter.format(project.orderCount || 0)} commande(s), toutes périodes`, icon: "↗", color: "#21865d" })}
        ${renderKpiCard({ label: "CA net facturé HT", value: compactCurrency(netRevenue), meta: `${formatCurrency(invoiced)} facturé · ${formatCurrency(credited)} d'avoirs`, icon: "€", color: "#5671d8" })}
        ${renderKpiCard({ label: "Coût main-d’œuvre cumulé HT", value: compactCurrency(cumulativeCost), meta: "Toutes périodes Odoo", icon: "€", color: "#2f8f83" })}
        ${renderKpiCard({ label: "Autres coûts cumulés HT", value: compactCurrency(otherCosts), meta: `${numberFormatter.format(project.fixedCostRows || 0)} écriture(s) analytique(s)`, icon: "◆", color: "#a064c7" })}
        ${renderKpiCard({ label: "Temps passé global", value: formatHours(globalHours), meta: "Toutes périodes Odoo", icon: "◷", color: "#f66729" })}
      </div>
    </section>
    <section class="dashboard-grid equal project-detail-grid">
      <article class="panel table-panel">
        <div class="table-toolbar"><div><h2>Commandes rattachées</h2><p>Correspondance exacte entre le numéro Sellsy et <code>x_num_sellsy</code>.</p></div><span class="table-count">${(project.orders || []).length} commande(s)</span></div>
        <div class="table-scroll"><table><thead><tr><th>Numéro</th><th>Date</th><th>Client commercial</th><th>Montant HT</th><th>Statut Sellsy</th></tr></thead><tbody>
          ${orderItems.length ? orderPager.rows.map((item) => { const status = sellsyOrderStatus[item.sellsyStatus] || { label: item.sellsyStatus || "Non renseigné", className: "archived" }; return `<tr><td><strong>${esc(item.number)}</strong></td><td>${esc(item.date || "—")}</td><td>${esc(item.commercialClient || "—")}</td><td>${esc(formatCurrency(item.amount))}</td><td><span class="pill ${status.className}">${esc(status.label)}</span></td></tr>`; }).join("") : '<tr><td colspan="5">Aucune commande Sellsy rapprochée.</td></tr>'}
        </tbody></table></div>
        <footer class="table-footer"><span>${orderPager.start}–${orderPager.end} sur ${orderItems.length}</span>${orderPager.html}</footer>
      </article>
      <article class="panel table-panel">
        <div class="table-toolbar"><div><h2>Factures et avoirs</h2><p>Documents attribués une seule fois via le projet Sellsy non ambigu.</p></div><span class="table-count">${(project.invoices || []).length + (project.creditNotes || []).length} document(s)</span></div>
        <div class="table-scroll"><table><thead><tr><th>Type</th><th>Numéro</th><th>Date</th><th>Montant HT</th><th>Statut Sellsy</th></tr></thead><tbody>
          ${documentItems.length ? documentPager.rows.map((item) => { const status = sellsyDocumentStatus[item.sellsyStatus] || { label: item.sellsyStatus || "Non renseigné", className: "archived" }; return `<tr><td>${item.documentType}</td><td><strong>${esc(item.number)}</strong></td><td>${esc(item.date || "—")}</td><td>${item.documentType === "Avoir" ? "−" : ""}${esc(formatCurrency(item.amount))}</td><td><span class="pill ${status.className}">${esc(status.label)}</span></td></tr>`; }).join("") : '<tr><td colspan="5">Aucune facture ou avoir Sellsy rapproché.</td></tr>'}
        </tbody></table></div>
        <footer class="table-footer"><span>${documentPager.start}–${documentPager.end} sur ${documentItems.length}</span>${documentPager.html}</footer>
      </article>
    </section>
    <section class="panel">
      <header class="panel-header"><div class="panel-title"><h2>Évolution de la trésorerie du projet</h2><p>Facturation nette et coûts globaux cumulés sur toute la durée du projet.</p></div></header>
      ${projectCashFlowChart(project)}
    </section>
    <section class="dashboard-grid equal project-detail-grid tasks-department-grid">
      <article class="panel table-panel">
        <div class="table-toolbar task-table-toolbar"><div><h2>Tâches du projet</h2><p>Temps global comparé au temps prévu actuel dans Odoo.</p><div class="task-status-filters" aria-label="Filtrer les tâches par état">${taskStatusDefinitions.map((status) => `<button type="button" class="task-status-chip ${status.className} ${state.projectTaskStatus === status.value ? "is-active" : ""}" data-task-status="${status.value}" aria-pressed="${state.projectTaskStatus === status.value}"><span>${status.label}</span><strong>${numberFormatter.format(taskStatusCounts[status.value])}</strong></button>`).join("")}<button type="button" class="task-filter-reset" data-reset-task-status ${state.projectTaskStatus ? "" : "disabled"} aria-label="Réinitialiser le filtre des tâches" title="Réinitialiser le filtre">×</button></div></div><span class="table-count">${taskRows.length} tâches</span></div>
        <div class="table-scroll"><table><thead><tr><th>Tâche</th><th>Prévu actuel</th><th>Consommé global</th><th>Exercice</th><th>Reste / écart</th><th>Taux</th><th>État</th></tr></thead><tbody>
          ${filteredTaskRows.length ? taskPager.rows.map((task) => {
            const labels = { NORMAL: "Normal", WARNING: "À surveiller", OVERRUN: "Dépassement", NO_BUDGET: "Budget absent" };
            const classes = { WARNING: "warning", OVERRUN: "danger", NO_BUDGET: "warning" };
            const balance = task.remainingHours === null ? "—" : task.remainingHours >= 0 ? `${formatHours(task.remainingHours)} restantes` : `${formatHours(Math.abs(task.varianceHours))} dépassées`;
            return `<tr><td><strong>${esc(task.name)}</strong></td><td>${task.plannedHours ? esc(formatHours(task.plannedHours)) : "—"}</td><td>${esc(formatHours(task.hours))}</td><td>${esc(formatHours(task.periodHours))}</td><td>${esc(balance)}</td><td>${task.consumptionRate === null ? "—" : esc(formatPercent(task.consumptionRate))}</td><td><span class="pill ${classes[task.status] || ""}">${labels[task.status] || esc(task.status)}</span></td></tr>`;
          }).join("") : '<tr><td colspan="7">Aucune tâche ne correspond à ce filtre.</td></tr>'}
        </tbody></table></div>
        <footer class="table-footer"><span>${taskPager.start}–${taskPager.end} sur ${filteredTaskRows.length}${state.projectTaskStatus ? ` · ${taskRows.length} au total` : ""}</span>${taskPager.html}</footer>
      </article>
      <article class="panel">
        <header class="panel-header"><div class="panel-title"><h2>Coûts par département</h2><p>Détail cumulé sur toute la durée du projet.</p></div></header>
        <div class="stack-list department-costs">
          ${departmentRows.length ? departmentRows.map((department) => `<div class="stack-row"><span class="stack-label" title="${esc(department.name)}">${esc(department.name)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (department.period.cost / maxDepartmentCost) * 100)}%"></div></div><span class="stack-value">${esc(formatCurrency(department.period.cost))}<small>${esc(formatHours(department.period.hours))}</small></span></div>`).join("") : '<div class="empty-state"><p>Aucun coût départemental pour ce projet.</p></div>'}
        </div>
      </article>
    </section>`;
}


function integrationStatusLabel(status) {
  return {
    queued: "En attente",
    running: "En cours",
    succeeded: "Terminée",
    failed: "Échec",
  }[status] || status || "Inconnu";
}

function freshnessLabel(status) {
  return {
    fresh: "À jour",
    warning: "À surveiller",
    stale: "Obsolète",
    missing: "Absent",
  }[status] || status;
}

async function loadAdminStatus({ rerender = true } = {}) {
  if (state.adminLoading) return;
  state.adminLoading = true;
  try {
    const response = await fetch("/api/admin/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.adminData = await response.json();
    state.adminError = null;
    const statusDot = document.querySelector("#admin-status-dot");
    statusDot?.classList.add("is-online");
    statusDot?.classList.toggle(
      "is-warning",
      Boolean(state.adminData.freshness_counts.stale || state.adminData.freshness_counts.missing),
    );
    const running = state.adminData.history.some((job) => ["queued", "running"].includes(job.status));
    if (running) scheduleAdminPoll();
  } catch (error) {
    state.adminData = null;
    state.adminError = error;
    document.querySelector("#admin-status-dot")?.classList.remove("is-online", "is-warning");
  } finally {
    state.adminLoading = false;
    if (rerender && state.view === "admin") render();
  }
}

function scheduleAdminPoll() {
  window.clearTimeout(state.adminPollTimer);
  state.adminPollTimer = window.setTimeout(async () => {
    await loadAdminStatus({ rerender: state.view === "admin" });
  }, 2200);
}

async function monitorJob(jobId, successMessage, onComplete = null, { reloadReport = true } = {}) {
  try {
    const response = await fetch(`/api/admin/integrations/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || `HTTP ${response.status}`);
    if (["queued", "running"].includes(job.status)) {
      await loadAdminStatus({ rerender: state.view === "admin" });
      window.setTimeout(() => monitorJob(jobId, successMessage, onComplete, { reloadReport }), 1600);
      return;
    }
    await loadAdminStatus({ rerender: false });
    onComplete?.();
    if (job.status === "succeeded") {
      if (reloadReport) await loadReportData({ rerender: true });
      showToast(successMessage);
      return;
    }
    render();
    showToast(`Actualisation en échec : ${job.error || "erreur inconnue"}`);
  } catch (error) {
    onComplete?.();
    showToast(`Suivi de l’actualisation impossible : ${error.message}`);
  }
}

async function startReportRebuild() {
  const button = document.querySelector("#admin-report-rebuild");
  button.disabled = true;
  button.innerHTML = '<span class="button-spinner"></span> Recalcul en cours';
  try {
    const response = await fetch("/api/admin/report/rebuild", {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast("Le recalcul du reporting a démarré.");
    monitorJob(
      payload.id,
      "Le reporting utilise maintenant les dernières sources publiées.",
      () => {
        button.disabled = false;
        button.innerHTML = '<span aria-hidden="true">↻</span> Actualiser les rapports';
      },
    );
  } catch (error) {
    showToast(`Impossible d’actualiser le reporting : ${error.message}`);
    button.disabled = false;
    button.innerHTML = '<span aria-hidden="true">↻</span> Actualiser les rapports';
  }
}


async function startOdooIntegration() {
  const button = document.querySelector("#run-odoo-import");
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="button-spinner"></span> Démarrage…';
  }
  try {
    const response = await fetch("/api/admin/integrations/odoo-timesheets", {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast("L’export Odoo a démarré en arrière-plan.");
    await loadAdminStatus({ rerender: true });
    monitorJob(payload.id, "La nouvelle révision Odoo est publiée. Les rapports restent inchangés jusqu’à leur actualisation.", null, { reloadReport: false });
  } catch (error) {
    showToast(`Impossible de démarrer l’export : ${error.message}`);
    await loadAdminStatus({ rerender: true });
  }
}

async function startSellsyIntegration() {
  const button = document.querySelector("#run-sellsy-import");
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="button-spinner"></span> Démarrage…';
  }
  try {
    const response = await fetch("/api/admin/integrations/sellsy-sources", {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast("L’import Sellsy a démarré en arrière-plan.");
    await loadAdminStatus({ rerender: true });
    monitorJob(payload.id, "La nouvelle révision Sellsy est publiée. Les rapports restent inchangés jusqu’à leur actualisation.", null, { reloadReport: false });
  } catch (error) {
    showToast(`Impossible de démarrer l’import Sellsy : ${error.message}`);
    await loadAdminStatus({ rerender: true });
  }
}

function renderAdmin() {
  if (!state.adminData) {
    appView.innerHTML = `
      <section class="panel backend-offline">
        <div>
          <div class="backend-offline-icon">⚙</div>
          <h2>Backend d’administration indisponible</h2>
          <p>Cette vue nécessite le serveur Python de l’application. Le reste du tableau de bord continue de fonctionner en mode statique.</p>
          <code>python reporting/run_backend.py</code>
        </div>
      </section>`;
    return;
  }

  const admin = state.adminData;
  const history = admin.history || [];
  const latestOdooSuccess = history.find((job) => job.integration === "odoo_timesheets" && job.status === "succeeded");
  const latestSellsy = history.find((job) => job.integration === "sellsy_sources");
  const latestSellsySuccess = history.find((job) => job.integration === "sellsy_sources" && job.status === "succeeded");
  const runningJob = history.find((job) => ["queued", "running"].includes(job.status));
  const runningOdoo = runningJob?.integration === "odoo_timesheets" ? runningJob : null;
  const runningSellsy = runningJob?.integration === "sellsy_sources" ? runningJob : null;
  const credentialsReady = admin.backend.odoo_credentials_configured;
  const sellsyReady = admin.backend.sellsy_configured;
  const sources = [...admin.sources].sort((a, b) => {
    const rank = { missing: 0, stale: 1, warning: 2, fresh: 3 };
    return rank[a.status] - rank[b.status];
  });
  const staleTotal = admin.freshness_counts.stale + admin.freshness_counts.missing;
  const sellsyFiles = latestSellsy?.files || [
    { source: "invoices", status: "pending" },
    { source: "orders", status: "pending" },
    { source: "credit-notes", status: "pending" },
  ];
  const sellsyFileResults = sellsyFiles.map((file) => {
    const status = file.status === "succeeded" ? "Réussi" : file.status === "failed" ? "Échec" : file.status === "running" ? "En cours" : "En attente";
    const size = file.size_bytes === null || file.size_bytes === undefined ? "" : ` · ${formatBytes(file.size_bytes)}`;
    return `<span class="file-result ${esc(file.status || "pending")}"><strong>${esc(file.source)}</strong><span>${esc(status + size)}</span></span>`;
  }).join("");

  appView.innerHTML = `
    <section class="admin-hero">
      <div class="integration-stack">
        <article class="panel integration-card">
          <div class="integration-logo">O</div>
          <div class="integration-info">
            <h2>Acquérir les sources Odoo</h2>
            <p>Extraction Odoo indépendante et strictement en lecture seule. Une nouvelle révision immuable est publiée après validation.</p>
            <div class="integration-meta">
              <span class="meta-chip">Odoo XML-RPC</span>
              <span class="meta-chip">${esc(admin.backend.active_sources?.odoo?.revisionId || "migration")}</span>
              <span class="meta-chip">${latestOdooSuccess ? numberFormatter.format(latestOdooSuccess.rows || 0) + " lignes" : "aucune exécution"}</span>
            </div>
          </div>
          <button class="secondary-button" id="run-odoo-import" type="button" ${!credentialsReady || runningJob ? "disabled" : ""}>
            ${runningOdoo ? '<span class="button-spinner"></span> Acquisition en cours' : "Acquérir Odoo"}
          </button>
        </article>
        <article class="panel integration-card">
          <div class="integration-logo">S</div>
          <div class="integration-info">
            <h2>Acquérir les sources Sellsy</h2>
            <p>Extraction Sellsy V2 indépendante et strictement en lecture seule, avec le détail des commandes nécessaire à l’analyse locale.</p>
            <div class="integration-meta">
              <span class="meta-chip">Sellsy API V2</span>
              <span class="meta-chip">${esc(admin.backend.active_sources?.sellsy?.revisionId || "migration")}</span>
              <span class="meta-chip">${latestSellsySuccess ? formatDateTime(latestSellsySuccess.completed_at) : "aucune exécution"}</span>
            </div>
          </div>
          <button class="secondary-button" id="run-sellsy-import" type="button" ${!sellsyReady || runningJob ? "disabled" : ""}>
            ${runningSellsy ? '<span class="button-spinner"></span> Acquisition en cours' : "Acquérir Sellsy"}
          </button>
        </article>
        <article class="panel integration-card report-rebuild-card">
          <div class="integration-logo">Rapport</div>
          <div class="integration-info">
            <h2>Actualiser les rapports</h2>
            <p>Recalcule les rapports et l’analyse Sellsy → Odoo uniquement depuis les révisions locales actives. Aucun appel API n’est envoyé.</p>
            <div class="integration-meta">
              <span class="meta-chip">Traitement local</span>
              <span class="meta-chip">Sans extraction ERP</span>
              <span class="meta-chip">Publication atomique data.json</span>
            </div>
          </div>
          <button class="secondary-button" id="admin-report-rebuild" type="button" ${runningJob ? "disabled" : ""}>
            ${runningJob?.integration === "report_rebuild" ? '<span class="button-spinner"></span> Recalcul en cours' : "↻ Actualiser les rapports"}
          </button>
        </article>
      </div>
      <article class="panel admin-summary">
        <div class="admin-stat"><strong>${admin.freshness_counts.fresh}</strong><span>sources à jour</span></div>
        <div class="admin-stat"><strong>${admin.freshness_counts.warning}</strong><span>sources à surveiller</span></div>
        <div class="admin-stat"><strong style="color:${staleTotal ? "#c94343" : "#21865d"}">${staleTotal}</strong><span>sources obsolètes ou absentes</span></div>
        <div class="admin-stat"><strong>${latestOdooSuccess ? numberFormatter.format(latestOdooSuccess.rows) : "—"}</strong><span>lignes du dernier export réussi</span></div>
      </article>
    </section>

    ${!credentialsReady ? '<div class="alert-banner"><span class="alert-symbol">!</span><p><strong>Identifiants Odoo non configurés.</strong> Renseignez ODOO_LOGIN et ODOO_PASSWORD dans le fichier .env situé à la racine, puis cliquez sur Actualiser.</p></div>' : ""}
    ${!sellsyReady ? `<div class="alert-banner"><span class="alert-symbol">!</span><p><strong>API Sellsy non configurée.</strong> ${esc(admin.backend.sellsy_configuration_error || "Renseignez SELLSY_CLIENT_ID et SELLSY_CLIENT_SECRET dans le fichier .env.")}</p></div>` : ""}

    <section class="panel table-panel" style="margin-bottom:14px">
      <div class="table-toolbar"><div><h2>Fraîcheur des fichiers d’alimentation</h2><p>Le seuil attendu varie selon la fréquence métier de chaque source.</p></div><span class="table-count">${sources.length} sources suivies</span></div>
      <div class="table-scroll"><table>
        <thead><tr><th>Source</th><th>Format</th><th>Dernière modification</th><th>Fraîcheur</th><th>Taille</th><th>Lignes connues</th><th>État</th></tr></thead>
        <tbody>${sources.map((source) => `<tr>
          <td><div class="source-name"><span class="source-logo ${source.system.includes("Sellsy") ? "sellsy" : "azure"}">${source.system.startsWith("Sellsy") ? "S" : "O"}</span><div class="entity-label"><strong>${esc(source.name)}</strong><span>${esc(source.relative_path)}</span></div></div></td>
          <td>${esc(source.format)}</td>
          <td>${esc(formatDateTime(source.modified_at))}</td>
          <td><div class="source-freshness"><span class="freshness-indicator ${esc(source.status)}"></span><div class="freshness-copy"><strong>${esc(formatAge(source.age_hours))}</strong><span>Attendu sous ${numberFormatter.format(source.expected_hours)} h</span></div></div></td>
          <td>${esc(formatBytes(source.size_bytes))}</td>
          <td>${source.records === null || source.records === undefined ? "—" : numberFormatter.format(source.records)}</td>
          <td><span class="pill ${source.status === "fresh" ? "" : source.status === "warning" ? "warning" : "danger"}">${esc(freshnessLabel(source.status))}</span></td>
        </tr>`).join("")}</tbody>
      </table></div>
    </section>

    <section class="panel table-panel">
      <div class="table-toolbar"><div><h2>Historique des intégrations</h2><p>Les erreurs sont conservées sans identifiant, mot de passe ni secret API.</p></div><span class="table-count">${history.length} exécutions</span></div>
      <div class="table-scroll"><table>
        <thead><tr><th>Démarrage</th><th>Intégration</th><th>Statut</th><th>Étape</th><th>Lignes / fichiers</th><th>Durée</th><th>Fichier / erreur</th></tr></thead>
        <tbody>${history.length ? history.map((job) => `<tr>
          <td>${esc(formatDateTime(job.started_at || job.created_at))}</td>
          <td><strong>${job.integration === "full_sources_snapshot" ? "Instantané Sellsy & Odoo" : job.integration === "odoo_timesheets" ? "Sources Odoo" : job.integration === "sellsy_sources" ? "Sources Sellsy" : job.integration === "report_rebuild" ? "Recalcul du reporting" : esc(job.integration)}</strong></td>
          <td><span class="job-status ${esc(job.status)}">${esc(integrationStatusLabel(job.status))}</span></td>
          <td>${esc(job.stage || "—")}</td>
          <td>${job.integration === "sellsy_sources" && Number(job.total || 0) > 0
            ? `${numberFormatter.format(job.processed || 0)} / ${numberFormatter.format(job.total || 0)}`
            : job.integration === "sellsy_sources"
              ? `${(job.files || []).filter((file) => file.status === "succeeded").length} / 3`
              : numberFormatter.format(job.rows || 0)}</td>
          <td>${esc(formatDuration(job.duration_seconds))}</td>
          <td>${job.error ? `<span class="job-error" title="${esc(job.error)}">${esc(job.error)}</span>` : esc(job.file_name || "—")}</td>
        </tr>`).join("") : '<tr><td colspan="7">Aucune intégration exécutée depuis ce backend.</td></tr>'}</tbody>
      </table></div>
    </section>`;
}

function render() {
  if (!state.data) return;
  updateHeader();
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === state.view);
  });
  const projectDetailOpen = state.view === "projects" && Boolean(state.selectedProjectId);
  document.querySelector(".filter-bar").hidden = ["projects", "admin"].includes(state.view);
  document.querySelector("#period-filter-field").hidden = false;
  document.querySelector("#bu-filter-field").hidden = state.view === "collaborators" || projectDetailOpen;
  document.querySelector("#search-filter-field").hidden = state.view === "overview" || projectDetailOpen;
  if (state.view === "overview") renderOverview();
  if (state.view === "collaborators") renderCollaborators();
  if (state.view === "projects") {
    if (state.selectedProjectId) renderProjectDetail();
    else renderProjects();
  }
  if (state.view === "admin") renderAdmin();
}

function setView(view) {
  if (!["overview", "collaborators", "projects", "admin"].includes(view)) return;
  state.view = view;
  state.page = 1;
  state.selectedProjectId = null;
  document.body.classList.remove("menu-open");
  render();
  if (view === "admin") loadAdminStatus({ rerender: true });
  else window.clearTimeout(state.adminPollTimer);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.setTimeout(() => toast.classList.remove("is-visible"), 2800);
}

function exportCsv() {
  let headers;
  let rows;
  if (state.view === "collaborators") {
    const months = collaboratorPeriodMonths();
    headers = ["Collaborateur", "Métier", ...months.map((month) => formatMonthLabel(month, "long")), "Total", "Lecture"];
    rows = filteredCollaborators().map((person) => [
      person.name,
      person.department,
      ...months.map((month) => person.control.monthByKey.get(month)?.hours ?? 0),
      person.control.totalHours,
      person.status[0],
    ]);
  } else if (state.view === "projects") {
    headers = ["Projet", "Client final Odoo", "Type de projet", "Responsable", "État Odoo", "Heures cumulées", "Coût MO cumulé", "Coûts fixes cumulés", "Coût global", "Commandé HT", "Facturé HT", "Avoirs HT", "CA net", "Marge globale"];
    rows = filteredProjects().map((project) => [project.name, project.client, project.type, project.manager, project.odooStatus, project.globalHours, project.laborCost, project.fixedCosts, project.globalCost, project.orderAmount, project.invoiceAmount, project.creditAmount, project.netRevenue, project.totalMargin]);
  } else if (state.view === "admin" && state.adminData) {
    headers = ["Source", "Système", "Format", "Dernière modification", "Âge (heures)", "Taille", "État"];
    rows = state.adminData.sources.map((source) => [source.name, source.system, source.format, source.modified_at, source.age_hours, source.size_bytes, source.status]);
  } else {
    headers = ["Mois", "Business unit", "Commandé HT", "Factures HT", "Avoirs HT", "CA net facturé HT", "Coût main-d’œuvre HT", "Autres coûts HT", "Coûts totaux HT", "Marge opérationnelle"];
    const months = new Set(activeMonths());
    rows = state.data.monthlyBu
      .filter((row) => months.has(row.month) && (state.bu === "all" || row.bu === state.bu))
      .map((row) => [row.month, row.bu, row.orders, row.invoiceAmount, row.creditAmount, row.revenue, row.laborCost ?? row.cost, row.otherCosts, row.totalCost ?? (Number(row.cost || 0) + Number(row.otherCosts || 0)), Number(row.revenue || 0) - Number(row.totalCost ?? (Number(row.cost || 0) + Number(row.otherCosts || 0)))]);
  }
  const csv = [headers, ...rows]
    .map((row) => row.map((cell) => `"${String(cell ?? "").replaceAll('"', '""')}"`).join(";"))
    .join("\n");
  const blob = new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `mandarine-${state.view}-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
  showToast("Export CSV généré avec les filtres actifs.");
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  periodFilter.addEventListener("change", () => {
    state.period = periodFilter.value;
    state.page = 1;
    render();
  });
  buFilter.addEventListener("change", () => {
    state.bu = buFilter.value;
    state.page = 1;
    render();
  });
  searchFilter.addEventListener("input", () => {
    state.search = searchFilter.value.trim();
    state.page = 1;
    render();
  });
  document.querySelector("#reset-filters").addEventListener("click", () => {
    state.period = state.data.meta.defaultPeriod || state.data.meta.fiscalPeriods?.at(-1)?.value || "fy";
    state.bu = "all";
    state.search = "";
    state.projectGlobalFilters.type = "Client";
    state.collaboratorInsufficientOnly = false;
    state.page = 1;
    periodFilter.value = state.period;
    buFilter.value = "all";
    searchFilter.value = "";
    render();
  });
  document.querySelector("#open-sources").addEventListener("click", () => setView("admin"));
  document.querySelector("#export-button").addEventListener("click", exportCsv);
  document.querySelector("#mobile-menu").addEventListener("click", () => document.body.classList.toggle("menu-open"));
  appView.addEventListener("click", (event) => {
    const collaboratorInsufficientButton = event.target.closest("[data-collaborator-insufficient]");
    if (collaboratorInsufficientButton) {
      state.collaboratorInsufficientOnly = !state.collaboratorInsufficientOnly;
      state.page = 1;
      render();
      return;
    }
    const reportRebuildButton = event.target.closest("#admin-report-rebuild");
    if (reportRebuildButton && !reportRebuildButton.disabled) startReportRebuild();
    const runButton = event.target.closest("#run-odoo-import");
    if (runButton && !runButton.disabled) startOdooIntegration();
    const runSellsyButton = event.target.closest("#run-sellsy-import");
    if (runSellsyButton && !runSellsyButton.disabled) startSellsyIntegration();
    const viewButton = event.target.closest("[data-go-view]");
    if (viewButton) setView(viewButton.dataset.goView);
    const pageButton = event.target.closest("[data-page]");
    if (pageButton) {
      state.page = Number(pageButton.dataset.page);
      render();
    }
    const projectDetailPageButton = event.target.closest("[data-project-detail-page]");
    if (projectDetailPageButton) {
      state.projectDetailPages[projectDetailPageButton.dataset.projectDetailKey] = Number(projectDetailPageButton.dataset.projectDetailPage);
      render();
    }
    const taskStatusButton = event.target.closest("[data-task-status]");
    if (taskStatusButton) {
      state.projectTaskStatus = taskStatusButton.dataset.taskStatus;
      state.projectDetailPages.tasks = 1;
      render();
      return;
    }
    if (event.target.closest("[data-reset-task-status]")) {
      state.projectTaskStatus = null;
      state.projectDetailPages.tasks = 1;
      render();
      return;
    }
    const sortButton = event.target.closest("[data-sort-key]");
    if (sortButton) {
      const view = sortButton.dataset.sortView;
      const key = sortButton.dataset.sortKey;
      const current = state.sorts[view];
      state.sorts[view] = { key, direction: current.key === key && current.direction === "asc" ? "desc" : "asc" };
      state.page = 1;
      render();
    }
    const removeProjectFilter = event.target.closest("[data-remove-project-filter]");
    if (removeProjectFilter) {
      state.projectGlobalFilters[removeProjectFilter.dataset.removeProjectFilter] = null;
      state.page = 1;
      render();
      return;
    }
    const projectRow = event.target.closest("[data-project-id]");
    if (projectRow) {
      state.selectedProjectId = projectRow.dataset.projectId;
      state.projectDetailPages = { orders: 1, documents: 1, tasks: 1 };
      state.projectTaskStatus = null;
      state.page = 1;
      render();
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    if (event.target.closest("[data-back-projects]")) {
      state.selectedProjectId = null;
      state.projectDetailPages = { orders: 1, documents: 1, tasks: 1 };
      state.projectTaskStatus = null;
      render();
    }
  });
  appView.addEventListener("input", (event) => {
    const input = event.target.closest("[data-filter-key]");
    if (!input) return;
    const view = input.dataset.filterView;
    const key = input.dataset.filterKey;
    state.columnFilters[view][key] = input.value;
    state.page = 1;
    render();
    const replacement = appView.querySelector(`[data-filter-view="${view}"][data-filter-key="${key}"]`);
    if (replacement) {
      replacement.focus();
      replacement.setSelectionRange(replacement.value.length, replacement.value.length);
    }
  });
  appView.addEventListener("change", (event) => {
    const lifecycleFilter = event.target.closest("[data-project-lifecycle]");
    if (!lifecycleFilter) return;
    state.projectLifecycle = lifecycleFilter.value;
    state.page = 1;
    render();
  });
  appView.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const projectRow = event.target.closest("[data-project-id]");
    if (projectRow) {
      event.preventDefault();
      state.selectedProjectId = projectRow.dataset.projectId;
      state.projectDetailPages = { orders: 1, documents: 1, tasks: 1 };
      state.projectTaskStatus = null;
      render();
    }
  });
}

async function initialise() {
  try {
    await loadReportData({ rerender: false });
    bindEvents();
    render();
    loadAdminStatus({ rerender: false });
  } catch (error) {
    appView.innerHTML = `<div class="empty-state"><div><h2>Impossible de charger les données</h2><p>${esc(error?.message || "Une erreur inattendue est survenue.")}</p><p>Vérifiez que le site est lancé via le serveur local.</p></div></div>`;
    console.error(error);
  }
}

initialise();
