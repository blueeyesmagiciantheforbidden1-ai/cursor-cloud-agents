"use strict";
(() => {
  const names = {codex: "Codex", claude: "Claude Code", cursor: "Cursor", copilot: "GitHub Copilot", grok: "Grok Build"};
  const initials = {codex: "Co", claude: "Cl", cursor: "Cu", copilot: "Gh", grok: "Gr"};
  const labels = {idle: "Ready", busy: "Working", offline: "Offline", unknown: "No telemetry", unconfigured: "Not connected", available: "Available", unavailable: "Unavailable", stale: "Stale", auth_failed: "Auth failed", queued: "Queued", running: "Running", completed: "Completed", failed: "Failed", stalled: "Stalled", cancelled: "Cancelled"};
  const tones = {idle: "good", completed: "good", available: "good", busy: "busy", running: "busy", queued: "busy", offline: "error", failed: "error", auth_failed: "error", stalled: "warning", stale: "warning"};
  const errors = {unconfigured: "The cloud hub connection is not configured yet.", not_cloud: "A cloud hub has not been connected. Preview or local hub data is excluded.", auth_failed: "The dashboard could not authenticate to the cloud hub.", unreachable: "The cloud hub did not respond.", hub_error: "The cloud hub could not provide a status snapshot.", invalid_response: "The cloud hub returned an invalid status snapshot."};
  const $ = id => document.getElementById(id);
  const set = (id, value) => { $(id).textContent = value; };
  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const number = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) : "—";
  const date = value => { const d = value ? new Date(value) : null; return d && Number.isFinite(d.getTime()) ? d : null; };
  const relative = value => { const d = date(value); if (!d) return "Not yet reported"; const seconds = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000)); return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` : `${Math.floor(seconds / 86400)}d ago`; };
  const badge = state => node("span", `badge ${tones[state] || ""}`, labels[state] || "Unknown");
  const empty = (title, text) => { const e = node("p", "empty-state"); e.append(node("strong", "", title), document.createTextNode(text)); return e; };
  const detail = (list, label, value) => { const row = node("div"); row.append(node("dt", "", label), node("dd", "", value)); list.append(row); };
  let latest = null, fetching = false, fetchError = false;

  function render(data) {
    const meta = data.dashboard || {}, cloud = data.cloud || {}, hub = data.hub || {};
    const connected = !fetchError && meta.hub_reachable === true && cloud.hub_connection === "connected";
    const cached = !connected && Boolean(meta.last_success_at);
    const agents = Object.keys(names).map(id => (data.agents || []).find(a => a.id === id) || {id, status: "unconfigured", configured: false, auth_status: "unknown"});
    const counts = (data.queue || {}).counts || {}, usage = data.usage || [];
    const reporting = agents.filter(a => ["idle", "busy"].includes(a.status)).length;
    const alerts = [...(data.alerts || [])];
    if (!connected) alerts.unshift({severity: meta.error === "unconfigured" || meta.error === "not_cloud" ? "info" : "critical", title: "Cloud hub not connected", message: fetchError ? "The browser could not refresh cloud status. Check your connection or Google sign-in." : errors[meta.error] || "Waiting for a cloud status connection.", observed_at: meta.fetched_at});
    const rank = {critical: 0, warning: 1, info: 2};
    alerts.sort((a, b) => (rank[a.severity] ?? 2) - (rank[b.severity] ?? 2));
    const critical = alerts.filter(a => a.severity === "critical").length;
    const attention = alerts.filter(a => ["critical", "warning"].includes(a.severity)).length;
    set("reporting-count", connected ? reporting : "—");
    set("reporting-total", `of ${agents.length} teammates`);
    set("worker-summary", connected ? "Recent authenticated worker heartbeat" : "Cloud worker telemetry not connected");
    set("running-count", connected ? number(counts.running ?? 0) : "—");
    set("queue-detail", connected || cached ? `${number(counts.queued ?? 0)} queued · ${number(counts.stalled ?? 0)} stalled${cached ? " · last snapshot" : ""}` : "Cloud queue not connected");
    set("alert-count", attention);
    set("alert-label", attention === 1 ? "active alert" : "active alerts");
    set("alert-summary", critical ? `${critical} critical · ${attention - critical} warning` : attention ? `${attention} warnings to review` : connected ? "No warning or critical alerts" : "Waiting for cloud monitoring");
    set("usage-count", connected ? usage.filter(u => u.status === "available").length : "—");
    set("nav-alerts", alerts.length); set("alerts-badge", alerts.length);
    set("deployment", connected ? "Cloud Run hub connected" : "Cloud hub not connected");
    set("updated", meta.last_success_at ? `Last received ${relative(meta.last_success_at)}` : "No cloud snapshot received");
    set("snapshot-time", date(data.generated_at) ? `Cloud snapshot ${date(data.generated_at).toLocaleString()}` : "No cloud snapshot received");
    const state = !connected ? cached || critical ? "error" : "waiting" : attention || hub.status === "degraded" ? "warning" : "";
    $("health-banner").className = `health-banner ${state}`;
    set("health-symbol", !connected ? cached || critical ? "!" : "···" : state === "warning" ? "!" : "✓");
    set("health-title", !connected ? "Cloud hub not connected" : state === "warning" ? "Your cloud hub needs attention" : "Your cloud hub is reporting");
    set("health-detail", !connected ? `${fetchError ? "The page could not refresh cloud status." : errors[meta.error] || "Waiting for the Cloud Run hub connection."} ${cached ? "The last cloud snapshot is retained below and marked stale." : "Agent activity and usage will appear after the cloud connection is verified."}` : `${reporting} of ${agents.length} registered agents have a recent worker heartbeat. Provider balances appear only when reported.`);
    set("cloud-observed", cloud.hub_last_seen ? `Hub last seen ${relative(cloud.hub_last_seen)}` : "No verified cloud connection");
    const resources = [
      {label: "Cloud hub", value: connected ? "Connected" : "Not connected", note: connected ? `${hub.service || "Hub"} · ${hub.store || "store unknown"}` : "Awaiting authenticated status"},
      {label: "Status website", value: cloud.dashboard_runtime === "cloud_run" ? "Cloud Run" : "Preview only", note: cloud.dashboard_runtime === "cloud_run" ? "Hosted in Google Cloud" : "Cloud website deployment not verified"},
      {label: "CPU / memory", value: "Not connected", note: "Cloud Monitoring metrics unavailable"},
      {label: "Instances / requests", value: "Not connected", note: "No cloud infrastructure metrics received"}
    ];
    $("cloud-grid").replaceChildren(...resources.map(resource => { const e = node("article", "resource"); e.append(node("span", "resource-label", resource.label), node("strong", "resource-value", resource.value), node("small", "resource-note", resource.note)); return e; }));
    $("agent-grid").replaceChildren(...agents.map(agent => {
      const e = node("article", "agent-card"), head = node("div", "agent-head"), list = node("dl", "agent-details");
      head.append(node("span", `agent-icon ${agent.id}`, initials[agent.id]), node("h3", "", names[agent.id]));
      detail(list, "Registered", connected || cached ? agent.configured ? "Yes" : "Not yet" : "Unknown");
      detail(list, "Heartbeat", relative(agent.last_seen));
      detail(list, "Provider auth", {verified: "Verified", failed: "Failed", unknown: "Unverified"}[agent.auth_status] || "Unverified");
      detail(list, "Last exit", number(agent.last_exit_code));
      if (agent.current_room_id) detail(list, "Cloud room", String(agent.current_room_id).slice(0, 10));
      e.append(head, badge(connected ? agent.status : cached && agent.configured ? "stale" : "unconfigured"), list);
      if (agent.error) e.append(node("p", "agent-error", agent.error));
      return e;
    }));
    $("alerts-list").replaceChildren(...(alerts.length ? alerts.map(alert => {
      const e = node("article", `alert ${["critical", "warning", "info"].includes(alert.severity) ? alert.severity : "info"}`), mark = node("span", "alert-mark", alert.severity === "info" ? "i" : "!"), copy = node("div");
      mark.setAttribute("aria-label", alert.severity || "info");
      copy.append(node("h3", "", alert.title || "Cloud status notice"), node("p", "", alert.message || ""), node("small", "", `${alert.agent_id ? `${names[alert.agent_id] || alert.agent_id} · ` : ""}${relative(alert.observed_at)}`));
      e.append(mark, copy); return e;
    }) : [empty("No alerts reported", "Cloud warnings and failures will appear here.")]));
    const rooms = data.rooms || [], queue = data.queue || {};
    set("room-window", `${number(queue.observed_rooms)} observed · ${typeof queue.window === "string" ? queue.window : "latest 50 rooms"}`);
    $("room-list").replaceChildren(...(rooms.length ? rooms.slice(0, 12).map(room => {
      const e = node("article", "room"), copy = node("div"), id = String(room.id || room.room_id || "Unknown room"), agent = room.current_agent || room.next_agent;
      const title = node("p", "room-name", id.length > 20 ? `${id.slice(0, 16)}…` : id); title.title = id;
      copy.append(title, node("p", "room-info", `${number(room.completed_steps)} / ${number(room.total_steps)} steps${agent ? ` · ${names[agent] || agent}` : ""} · ${relative(room.updated_at || room.created_at)}`));
      e.append(copy, badge(room.status)); return e;
    }) : [empty(connected ? "No cloud rooms yet" : "Cloud queue not connected", "Only work received by the cloud hub appears here.")]));
    $("quota-summary").replaceChildren(...usage.filter(u => u.metric === "quota_percent").map(u => {
      const e = node("article", "quota-card"), heading = node("div", "quota-heading"), amount = node("div", "quota-amount");
      heading.append(node("h3", "", u.label || `${names[u.provider] || u.provider} allowance`), badge(connected ? u.status : "stale"));
      amount.append(node("strong", "", typeof u.remaining === "number" ? `${number(u.remaining)}%` : "—"), node("span", "", "remaining"));
      e.append(heading, amount, node("p", "", `${number(u.used)}% used · ${u.window_minutes === 10080 ? "7-day window" : `${number(u.window_minutes)}-minute window`}`), node("small", "", `Reported ${relative(u.observed_at)} · Resets ${date(u.resets_at) ? date(u.resets_at).toLocaleString() : "unknown"}`)); return e;
    }));
    $("usage-body").replaceChildren(...(usage.length ? usage.map(u => {
      const row = node("tr"), provider = node("td"), metric = node("td", "", {budget_usd: "Budget", credits: "Credits", tokens: "Tokens", requests: "Requests", quota_percent: "Subscription allowance"}[u.metric] || u.metric || "Unknown");
      provider.append(node("strong", "", names[u.provider] || u.provider || "Unknown"), node("small", "", `${u.scope || "unknown"} scope`));
      if (u.label) metric.append(node("small", "", u.label));
      const values = [u.used, u.remaining, u.limit].map(v => node("td", typeof v === "number" ? "" : "usage-unavailable", `${number(v)}${typeof v === "number" && u.unit ? ` ${u.unit}` : ""}`));
      const source = node("td", "", u.source || "Not connected"), status = node("td");
      source.append(node("small", "", relative(u.observed_at))); status.append(badge(connected ? u.status : "stale"));
      if (u.error) status.title = u.error;
      row.append(provider, metric, ...values, source, status); return row;
    }) : [(() => { const row = node("tr"), cell = node("td", "empty-state", "Provider usage is not yet connected to the cloud hub. Balances are unknown."); cell.colSpan = 7; row.append(cell); return row; })()]));
  }

  async function refresh() {
    if (fetching) return;
    fetching = true; $("refresh").disabled = true; $("refresh").setAttribute("aria-busy", "true");
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/api/status", {headers: {Accept: "application/json"}, cache: "no-store", credentials: "same-origin", signal: controller.signal});
      if (!response.ok) throw new Error("Status unavailable");
      const data = await response.json();
      if (data.schema_version !== 1 || !Array.isArray(data.agents) || !Array.isArray(data.alerts) || !Array.isArray(data.usage)) throw new Error("Invalid status");
      latest = data; fetchError = false;
    } catch (_) { fetchError = true; }
    finally { clearTimeout(timeout); fetching = false; $("refresh").disabled = false; $("refresh").removeAttribute("aria-busy"); render(latest || {agents: [], alerts: [], usage: []}); }
  }
  $("refresh").addEventListener("click", refresh);
  $("auto-refresh").addEventListener("change", () => { if ($("auto-refresh").checked) refresh(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && $("auto-refresh").checked) refresh(); });
  setInterval(() => { if ($("auto-refresh").checked && !document.hidden) refresh(); }, 15000);
  refresh();
})();
