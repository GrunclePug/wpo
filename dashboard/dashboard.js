// wpo dashboard: renders the server's snapshots and journal events (server-sent events from
// /api/stream). No build step and no dependencies; every value from the server is escaped.
"use strict";

const STATUS_ORDER = ["in_progress", "changes_requested", "review", "blocked", "todo"];
const STATUS_LABEL = { in_progress: "in progress", changes_requested: "changes requested", review: "review", blocked: "blocked", todo: "todo", done: "done" };
const LANE_ATTENTION = new Set(["needs-orchestrator", "died", "crashed"]);
const FEED_LIMIT = 300;
const STALE_STREAM_MS = 40000;
const DETAIL_REFRESH_MS = 5000;

const ui = {
  snapshot: null,
  rowSignatures: new Map(),
  events: [],
  eventIndexes: new Set(),
  feedFilter: localStorage.getItem("wpo.feedFilter") || "all",
  collapsed: storedCollapsed(),
  filterText: "",
  source: null,
  lastMessageAt: 0,
  connected: false,
  detailId: null,
  detail: null,
  detailFetchedAt: 0,
  detailSignature: "",
  // Disclosure key -> open, as the operator left it, kept across refreshes of the open detail.
  detailToggles: new Map(),
  fault: null,
  // Text typed into action forms, by form key, kept across re-renders until it is sent.
  drafts: new Map(),
  // The last outcome of each form ({ ok, text }), and the forms whose action is in flight.
  outcomes: new Map(),
  pending: new Set(),
  // What each action area showed last; an area is rebuilt only when this changes, so typing is not interrupted.
  areaSignatures: new Map(),
};

// The server's per-start token, embedded in the page it served; every action sends it back.
const ACTION_TOKEN = document.querySelector('meta[name="wpo-action-token"]')?.content || "";
const UNBLOCKABLE = new Set(["blocked", "changes_requested", "review"]);
const TOAST_MS = 12000;

// ------------------------------------------------------------------------------------- helpers

function storedCollapsed() {
  try {
    const stored = JSON.parse(localStorage.getItem("wpo.collapsed") || "null");
    if (stored && typeof stored === "object") return stored;
  } catch {
    // An unreadable entry falls back to the defaults below.
  }
  return { waiting: true, "idle-lanes": true };
}

// A run-directory path as a URL path: each segment encoded, so '#' or '?' in a name stays in it.
function runPath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ESCAPES[character]);
}

const $ = (id) => document.getElementById(id);

function nowSeconds() { return Date.now() / 1000; }

function ago(epoch) {
  if (!epoch) return "—";
  const seconds = Math.max(0, nowSeconds() - epoch);
  if (seconds < 45) return "now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function duration(minutes) {
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${Math.round(minutes)} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${String(Math.round(minutes % 60)).padStart(2, "0")}m`;
  return `${Math.round(hours / 24)} days`;
}

function clock(epoch) {
  if (!epoch) return "";
  const date = new Date(epoch * 1000);
  const sameDay = date.toDateString() === new Date().toDateString();
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return sameDay ? time : `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function fullTime(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleString([], { hour12: false }) : "";
}

function tokens(count) {
  if (count == null) return "—";
  return count >= 1000 ? `${Math.round(count / 1000)}k` : String(count);
}

function bytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function eventTime(event) {
  const parsed = Date.parse(event.time);
  return Number.isNaN(parsed) ? null : parsed / 1000;
}

function timeTag(epoch, text) {
  return `<time datetime="${esc(new Date((epoch || 0) * 1000).toISOString())}" title="${esc(fullTime(epoch))}" data-ago="${epoch || ""}">${esc(text ?? ago(epoch))}</time>`;
}

function statusPill(status) {
  return `<span class="pill s-${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

function lanePill(state) {
  return `<span class="pill l-${esc(String(state).replace(/\s+/g, "-"))}">${esc(state)}</span>`;
}

function wpButton(id, extraClass = "") {
  return `<button type="button" class="wp-id ${extraClass}" data-open-wp="${esc(id)}">${esc(id)}</button>`;
}

function chevron() {
  return '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 3.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}

function saveCollapsed() { localStorage.setItem("wpo.collapsed", JSON.stringify(ui.collapsed)); }

// Re-rendering replaces markup; the focused control is found again by its key afterwards.
function keepFocus(render) {
  const active = document.activeElement;
  const key = active && active.dataset ? (active.dataset.openWp ? `wp:${active.dataset.openWp}:${active.closest("[id]")?.id}` : active.dataset.focusKey) : null;
  const selection = active && typeof active.selectionStart === "number" ? [active.selectionStart, active.selectionEnd] : null;
  render();
  if (!key) return;
  let target = null;
  if (key.startsWith("wp:")) {
    const [, id, container] = key.split(":");
    target = document.querySelector(`#${CSS.escape(container)} [data-open-wp="${CSS.escape(id)}"]`);
  } else {
    target = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
  }
  if (target && target !== document.activeElement) {
    target.focus({ preventScroll: true });
    if (selection && typeof target.setSelectionRange === "function") target.setSelectionRange(...selection);
  }
}

// ------------------------------------------------------------------------------------ the stream

function connect() {
  if (ui.source) ui.source.close();
  const source = new EventSource("/api/stream");
  ui.source = source;
  source.addEventListener("open", () => { ui.connected = true; ui.lastMessageAt = Date.now(); renderConnection(); });
  source.addEventListener("snapshot", (message) => {
    ui.lastMessageAt = Date.now();
    if (!message.data || message.data === "null") return;
    receiveSnapshot(JSON.parse(message.data));
  });
  source.addEventListener("events", (message) => {
    ui.lastMessageAt = Date.now();
    const payload = JSON.parse(message.data);
    receiveEvents(payload.events, Boolean(payload.reset));
  });
  source.addEventListener("fault", (message) => {
    ui.lastMessageAt = Date.now();
    ui.fault = JSON.parse(message.data).error;
    renderBanner();
  });
  source.addEventListener("message", () => { ui.lastMessageAt = Date.now(); });
  source.onerror = () => { ui.connected = false; renderConnection(); };
}

// The server sends a heartbeat every 15 s; a silent stream is a dead one (a sleeping laptop,
// a restarted server), so it is replaced.
setInterval(() => {
  if (ui.connected && Date.now() - ui.lastMessageAt > STALE_STREAM_MS) {
    ui.connected = false;
    renderConnection();
    connect();
  }
}, 5000);

function renderConnection() {
  const element = $("connection");
  const text = element.querySelector(".connection-text");
  if (ui.connected) {
    element.dataset.state = "live";
    text.textContent = ui.snapshot ? `Live · ${ago(ui.lastMessageAt / 1000)}` : "Live";
  } else if (ui.snapshot) {
    element.dataset.state = Date.now() - ui.lastMessageAt > 60000 ? "down" : "reconnecting";
    text.textContent = element.dataset.state === "down" ? `No server for ${ago(ui.lastMessageAt / 1000)}` : "Reconnecting…";
  } else {
    element.dataset.state = "reconnecting";
    text.textContent = "Connecting…";
  }
  renderBanner();
}

function renderBanner() {
  const banner = $("banner");
  if (!ui.connected && ui.snapshot && Date.now() - ui.lastMessageAt > 60000) {
    banner.innerHTML = "The dashboard server is not answering; the board below is as it last was. <code>wpo dashboard</code> starts it again, and any wpo command does too unless it was stopped with <code>wpo dashboard --stop</code>.";
    banner.hidden = false;
  } else if (ui.fault) {
    banner.innerHTML = `The server could not read the run state on its last pass: <code>${esc(ui.fault)}</code>. It keeps trying; <code>.orchestration/runs/dashboard.log</code> has the details.`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

// ---------------------------------------------------------------------------------- snapshots

function rowSignature(row) {
  return [row.status, row.steps_done, row.rounds, row.failures, row.evaluations, row.latest_progress,
    row.session && row.session.role, row.session && row.session.route, row.open_findings.length].join("|");
}

function receiveSnapshot(snapshot) {
  const first = ui.snapshot === null;
  const changed = new Set();
  for (const row of snapshot.wps) {
    const signature = rowSignature(row);
    if (!first && ui.rowSignatures.get(row.id) !== signature) changed.add(row.id);
    ui.rowSignatures.set(row.id, signature);
  }
  ui.snapshot = snapshot;
  keepFocus(() => renderBoard(changed));
  if (first && location.hash === "#needs-you") $("needs-disclosure").open = true;
  // The board arrives after load, so a section anchor in the address is honoured once it exists.
  if (first && location.hash.length > 1 && !location.hash.startsWith("#/")) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
  renderConnection();
  if (ui.detailId) refreshDetailIfChanged();
}

function renderBoard(changed) {
  const snapshot = ui.snapshot;
  document.title = `${snapshot.project.name} · wpo`;
  $("project-name").textContent = snapshot.project.name;
  renderUsage(snapshot.usage);
  renderNativeBackend(snapshot.native_backend);
  renderAttention(snapshot);
  renderNeedsYou(snapshot);
  renderRequestBox();
  renderLanes(snapshot);
  renderWorkPackages(snapshot, changed);
  renderMilestones(snapshot.milestones);
  renderMerged(snapshot.merged);
}

function renderNativeBackend(native) {
  const panel = $("native-backend");
  $("native-disclosure").hidden = !native;
  if (!native) return;
  $("native-summary").textContent = `T3 execution · ${native.pending} outstanding${native.stalled ? ` · ${native.stalled} stalled` : ""}`;
  const tasks = native.tasks.map((task) => {
    const href = nativeThreadLink(task.link);
    const link = href ? `<a href="${esc(href)}">Open in T3</a>` : "";
    return `<li><strong>${esc(task.wp || task.kind)}</strong> ${esc(task.role || "")} · ${esc(task.state)} ${link}${task.native_approval ? " · Approval in T3" : ""}${task.detail ? `<p>${esc(task.detail)}</p>` : ""}</li>`;
  }).join("");
  panel.innerHTML = `<div class="panel-head"><span>${native.coordinator_thread_id ? "Coordinator attached" : "Attach a coordinator from a T3 repository thread"}</span><button type="button" class="action-button" id="t3-recover" data-focus-key="t3-recover" ${native.pending && !ui.pending.has("t3-recover") ? "" : "disabled"}>Recover coordinator</button></div>${tasks ? `<ul>${tasks}</ul>` : ""}<p>Recovery wakes coordinators once and keeps held lanes paused. Native permission requests are answered in T3.</p>`;
  $("t3-recover").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    ui.pending.add("t3-recover");
    const outcome = await postAction("t3-recover", {});
    ui.pending.delete("t3-recover");
    toast("T3 coordinator:", outcome);
    refreshState();
  });
}

function nativeThreadLink(value) {
  let href = String(value || "").trim();
  if (href.startsWith("<") && href.endsWith(">")) href = href.slice(1, -1);
  return /^(https?:|t3:|t3-thread:)/i.test(href) ? href : "";
}

// -------------------------------------------------------------------------------------- usage

function renderUsage(usage) {
  const parts = [];
  const summaries = [];
  for (const family of usage.families) {
    const name = family.family === "gpt" ? "GPT" : family.family.charAt(0).toUpperCase() + family.family.slice(1);
    const summary = family.windows.map((window) => `${window.label} ${Math.round(window.used_percent)}%`).join(", ");
    summaries.push(`${name} ${summary || "unknown"}`);
    const windows = family.windows.map((window) => usageWindow(window)).join("");
    const unknown = family.windows.length === 0 ? `<span class="forecast is-soon">usage unknown${family.error ? `: ${esc(family.error)}` : ""}</span>` : "";
    parts.push(`<div class="family"><span class="family-name">${esc(name)}${family.plan ? `<small>${esc(family.plan)}</small>` : ""}</span>${windows}${unknown}${forecast(family.forecast, family.windows, usage.lead_minutes)}</div>`);
  }
  const age = usage.fetched_at ? nowSeconds() - usage.fetched_at : null;
  const stale = age !== null && age > 900;
  parts.push(`<span class="forecast${stale ? " is-soon" : ""}" title="When usage_budget.py last read the usage endpoints">read ${usage.fetched_at ? timeTag(usage.fetched_at) : "never"}${stale ? " (stale)" : ""}</span>`);
  if (usage.cooldowns.length) {
    const items = usage.cooldowns.map((entry) => `<li><span class="key">${esc(entry.key)}</span> until ${esc(clock(entry.until))} <span class="dim">(in ${esc(duration((entry.until - nowSeconds()) / 60))})</span><div class="reason">${esc(entry.reason)}</div></li>`).join("");
    parts.push(`<button type="button" class="cooldowns-button" popovertarget="cooldowns" data-focus-key="cooldowns">${usage.cooldowns.length} cooldown${usage.cooldowns.length === 1 ? "" : "s"}</button>
      <div class="cooldowns-popover" id="cooldowns" popover><h3>Route cooldowns</h3><ul>${items}</ul></div>`);
  }
  const container = $("usage");
  $("usage-summary").textContent = `Usage · ${summaries.join(" · ")}`;
  const popoverOpen = container.querySelector("#cooldowns:popover-open") !== null;
  container.innerHTML = parts.join("");
  if (popoverOpen) container.querySelector("#cooldowns")?.showPopover();
}

function usageWindow(window) {
  const used = window.used_percent;
  const level = used >= 100 ? "is-capped" : used >= window.ceiling ? "is-over" : "";
  const width = Math.min(100, Math.max(0, used));
  const tick = Math.min(100, Math.max(0, window.ceiling));
  const limit = window.paced ? `pace ${Math.round(window.ceiling)}%` : `ceiling ${Math.round(window.ceiling)}%`;
  const label = `${window.label} usage ${used}%, ${limit}, resets ${clock(window.resets_at)}`;
  return `<div class="window" title="${esc(label)}">
      <span class="window-label">${esc(window.label)}</span>
      <span class="meter" role="meter" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}"><span class="meter-fill ${level}" style="width:${width}%"></span><span class="meter-tick" style="left:${tick}%"></span></span>
      <span class="window-value ${level}">${Math.round(used)}%</span>
      <span class="window-meta">${esc(limit)} · resets ${esc(clock(window.resets_at))}</span>
    </div>`;
}

function forecast(entry, windows, leadMinutes) {
  if (!entry) return "";
  // A cap the burn would reach only after the window resets never arrives.
  const window = windows.find((candidate) => candidate.label === entry.label);
  if (window && nowSeconds() + entry.minutes_to_cap * 60 >= window.resets_at) return "";
  const soon = entry.minutes_to_cap <= leadMinutes;
  return `<span class="forecast${soon ? " is-soon" : ""}" title="${esc(entry.text)}">${esc(entry.label)} cap in ${esc(duration(Math.max(0, entry.minutes_to_cap)))} at ${entry.percent_per_minute.toFixed(2)}%/min</span>`;
}

// ---------------------------------------------------------------------------------- attention

function renderAttention(snapshot) {
  const items = [];
  for (const lane of snapshot.lanes) {
    if (LANE_ATTENTION.has(lane.state) && lane.open_wps.length) {
      items.push(`<span class="item">lane <strong>${esc(lane.name)}</strong> ${esc(lane.state)}</span>`);
    }
  }
  const waiting = needsYouRows(snapshot).length + (snapshot.approvals || []).length;
  if (waiting) items.push(`<span class="item"><a href="#needs-you"><strong>${waiting}</strong> waiting on an answer</a></span>`);
  for (const [path, error] of Object.entries(snapshot.parse_errors || {})) {
    items.push(`<span class="item"><strong>unparsable WP file</strong> <span class="mono">${esc(path)}</span>: ${esc(error)}</span>`);
  }
  const strip = $("attention");
  strip.hidden = items.length === 0;
  $("attention-summary").textContent = `Needs attention · ${waiting} waiting on an answer · ${items.length - Number(Boolean(waiting))} lane or file alerts`;
  $("attention-items").innerHTML = items.join("");
}

// ------------------------------------------------------------------------------------- actions
// Forms post to /api/action/* with the page's token; the outcome shows in the form and as a toast,
// and the board and the open detail are fetched again. An area is rebuilt only when what it shows
// changed, and typed text is kept in ui.drafts, so a snapshot arriving mid-sentence loses nothing.

// WPs waiting on the user: blocked, or handed to the orchestrator while left in review.
function needsYouRows(snapshot) {
  return snapshot.wps.filter((row) => row.status === "blocked" || (row.held_by && !row.active && UNBLOCKABLE.has(row.status)));
}

function actionState(keys) {
  return keys.map((key) => [key, ui.pending.has(key), ui.outcomes.get(key)?.text ?? null]);
}

// The area name keeps focus keys unique when one form key shows in two places (the list and the detail).
function draftArea(area, key, placeholder, label, rows = 2) {
  const id = `draft-${area}-${key}`;
  return `<label class="visually-hidden" for="${esc(id)}">${esc(label)}</label><textarea id="${esc(id)}" class="action-text" rows="${rows}" data-draft="${esc(key)}" data-focus-key="${esc(`${area}:text:${key}`)}" placeholder="${esc(placeholder)}">${esc(ui.drafts.get(key) || "")}</textarea>`;
}

function actionButton(area, key, label, kind, verb = "") {
  return `<button type="submit" class="action-button ${kind}"${verb ? ` value="${esc(verb)}"` : ""} data-focus-key="${esc(`${area}:button:${key}:${verb}`)}"${ui.pending.has(key) ? " disabled" : ""}>${esc(label)}</button>`;
}

function outcomeLine(key) {
  if (ui.pending.has(key)) return '<p class="action-outcome" role="status">Sending…</p>';
  const outcome = ui.outcomes.get(key);
  if (!outcome) return "";
  return `<p class="action-outcome ${outcome.ok ? "is-ok" : "is-error"}" role="status">${esc(outcome.text)}</p>`;
}

function unblockForm(area, id, status) {
  const key = `unblock:${id}`;
  const chosen = ui.drafts.get(`${key}:status`) || (status === "review" ? "review" : "todo");
  const options = ["todo", "review"].map((value) => `<option value="${value}"${value === chosen ? " selected" : ""}>${value}</option>`).join("");
  return `<form class="action-form" data-action="unblock" data-key="${esc(key)}" data-wp="${esc(id)}">
    ${draftArea(area, key, "Optional note for the builder: it goes into the WP's Context as a dated User note", `Note for ${id}`)}
    <div class="action-row">
      <label class="inline-label">back to <select name="status" data-draft="${esc(key)}:status" data-focus-key="${esc(`${area}:status:${key}`)}">${options}</select></label>
      ${actionButton(area, key, "Unblock", "primary")}
    </div>
    ${outcomeLine(key)}
  </form>`;
}

function approvalForm(approval) {
  const key = `approval:${approval.id}`;
  return `<form class="action-form" data-action="approval" data-key="${esc(key)}" data-id="${esc(approval.id)}">
    ${draftArea("needs", key, "A note for the session (approve), or why not (deny: required)", `Answer to ${approval.id}`)}
    <div class="action-row">${actionButton("needs", key, "Approve", "primary", "approve")}${actionButton("needs", key, "Deny", "danger", "deny")}</div>
    ${outcomeLine(key)}
  </form>`;
}

function renderNeedsYou(snapshot) {
  const approvals = snapshot.approvals || [];
  const held = needsYouRows(snapshot);
  const panel = $("needs-you");
  panel.hidden = approvals.length === 0 && held.length === 0;
  const parts = [];
  if (approvals.length) parts.push(`${approvals.length} approval${approvals.length === 1 ? "" : "s"}`);
  if (held.length) parts.push(`${held.length} blocked`);
  $("needs-you-count").textContent = parts.join(" · ");
  const keys = [...approvals.map((approval) => `approval:${approval.id}`), ...held.map((row) => `unblock:${row.id}`)];
  const signature = JSON.stringify([approvals.map((approval) => approval.id), held.map((row) => [row.id, row.status, row.held_by, row.latest_progress]), actionState(keys)]);
  if (ui.areaSignatures.get("needs-you") === signature) return;
  ui.areaSignatures.set("needs-you", signature);
  const items = [
    ...approvals.map((approval) => `<li class="action-item">
      <div class="action-head">${wpButton(approval.wp)}<span class="pill plain s-changes_requested">approval</span><span class="mono dim">${esc(approval.id)}</span><span class="dim">${esc(approval.role || "session")}${approval.route ? ` on ${esc(approval.route)}` : ""} · asked ${esc(approval.requested.replace("T", " "))}</span></div>
      <pre class="action-command">${esc(approval.command)}</pre>
      <p class="action-why">${esc(approval.why)}</p>
      ${approvalForm(approval)}
    </li>`),
    ...held.map((row) => `<li class="action-item">
      <div class="action-head">${wpButton(row.id)}${statusPill(row.status)}<span class="wp-title" title="${esc(row.title)}">${esc(row.title)}</span></div>
      <p class="action-why">${row.held_by ? `<span class="mono">${esc(row.held_by)}</span> · ` : ""}${row.latest_progress ? progressLine(row.latest_progress) : "no progress logged"}</p>
      ${unblockForm("needs", row.id, row.status)}
    </li>`),
  ];
  keepFocus(() => { $("needs-you-list").innerHTML = items.join(""); });
}

function renderRequestBox() {
  const key = "request";
  const signature = JSON.stringify(actionState([key]));
  if (ui.areaSignatures.get("request") === signature) return;
  ui.areaSignatures.set("request", signature);
  keepFocus(() => {
    $("request-box").innerHTML = `<form class="action-form" data-action="message" data-key="${key}">
      <p class="dim">Work you want done. A placement agent (code-hard, never a free or local model) adds it to the open work package that handles it, or drafts a new one; the orchestrator is told.</p>
      ${draftArea("request", key, "What should be built or changed?", "Request", 3)}
      <div class="action-row">${actionButton("request", key, "Send request", "primary")}</div>
      ${outcomeLine(key)}
    </form>`;
  });
}

function renderDetailActions() {
  const container = $("detail-actions");
  const detail = ui.detail;
  if (!ui.detailId || !detail || detail.id !== ui.detailId) {
    container.innerHTML = "";
    ui.areaSignatures.delete("detail");
    return;
  }
  const row = ui.snapshot && ui.snapshot.wps.find((candidate) => candidate.id === detail.id);
  const status = detail.fields.status;
  const unblockable = status === "blocked" || status === "changes_requested" || Boolean(row && row.held_by && !row.active && UNBLOCKABLE.has(status));
  const messageKey = `message:${detail.id}`;
  const signature = JSON.stringify([detail.id, status, unblockable, detail.reads_messages, actionState([messageKey, `unblock:${detail.id}`])]);
  if (ui.areaSignatures.get("detail") === signature) return;
  ui.areaSignatures.set("detail", signature);
  const placeholder = detail.reads_messages ? "Message the running session; it reads it between turns"
    : status === "done" ? "Ask for follow-up work: a placement agent puts it in the open WP that handles it, or drafts a new one"
    : `No session reads messages now: a placement agent adds it to ${detail.id}'s Context`;
  const message = `<form class="action-form" data-action="message" data-key="${esc(messageKey)}" data-wp="${esc(detail.id)}">
    ${draftArea("detail", messageKey, placeholder, `Message about ${detail.id}`)}
    <div class="action-row">${detail.reads_messages ? '<span class="dim"><span class="live-dot"></span> a session is reading</span>' : ""}${actionButton("detail", messageKey, detail.reads_messages ? "Send to session" : "Send", "primary")}</div>
    ${outcomeLine(messageKey)}
  </form>`;
  keepFocus(() => {
    container.innerHTML = `${unblockable ? `<div class="detail-action"><h3>Unblock</h3>${unblockForm("detail", detail.id, status)}</div>` : ""}<div class="detail-action"><h3>Message</h3>${message}</div>`;
  });
}

function rerenderActionAreas() {
  if (ui.snapshot) renderNeedsYou(ui.snapshot);
  renderRequestBox();
  renderDetailActions();
}

async function postAction(name, body) {
  try {
    const response = await fetch(`/api/action/${name}`, {
      method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", "X-WPO-Token": ACTION_TOKEN }, body: JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = JSON.parse(text);
    } catch {
      // Not JSON (a proxy's or the HTTP layer's own page): shown as it came below.
    }
    if (payload && typeof payload === "object") return { ok: response.ok && payload.ok === true, text: payload.ok ? payload.message : payload.error };
    return { ok: false, text: `${response.status} ${response.statusText}: ${text}` };
  } catch (error) {
    return { ok: false, text: `The server did not answer: ${error.message}` };
  }
}

// The stream pushes the board after an action too; this covers a page whose stream is reconnecting.
async function refreshState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) return;
    const state = await response.json();
    if (state.snapshot) receiveSnapshot(state.snapshot);
    receiveEvents(state.events || [], false);
  } catch {
    // Unreachable now: the connection indicator says so, and the stream brings the board when it is back.
  }
}

function toast(subject, outcome) {
  const item = document.createElement("li");
  item.className = `toast ${outcome.ok ? "is-ok" : "is-error"}`;
  item.innerHTML = `<p><strong>${esc(subject)}</strong> ${esc(outcome.text)}</p><button type="button" class="icon-button" aria-label="Dismiss"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button>`;
  item.querySelector("button").addEventListener("click", () => item.remove());
  $("toasts").prepend(item);
  // An error stays until dismissed: it may need acting on.
  if (outcome.ok) setTimeout(() => item.remove(), TOAST_MS);
}

async function submitAction(form, submitter) {
  const key = form.dataset.key;
  if (ui.pending.has(key)) return;
  const text = form.querySelector("textarea").value;
  let name;
  let body;
  let subject;
  if (form.dataset.action === "unblock") {
    name = "unblock";
    body = { wp: form.dataset.wp, note: text, status: form.querySelector("select[name=status]").value };
    subject = `Unblock ${form.dataset.wp}:`;
  } else if (form.dataset.action === "approval") {
    name = submitter && submitter.value === "deny" ? "deny" : "approve";
    body = name === "deny" ? { id: form.dataset.id, reason: text } : { id: form.dataset.id, note: text };
    subject = `${name === "deny" ? "Deny" : "Approve"} ${form.dataset.id}:`;
  } else {
    name = "message";
    body = { wp: form.dataset.wp || null, text };
    subject = form.dataset.wp ? `Message to ${form.dataset.wp}:` : "Request:";
  }
  ui.pending.add(key);
  ui.outcomes.delete(key);
  rerenderActionAreas();
  const outcome = await postAction(name, body);
  ui.pending.delete(key);
  ui.outcomes.set(key, outcome);
  if (outcome.ok) {
    ui.drafts.delete(key);
    ui.drafts.delete(`${key}:status`);
  }
  toast(subject, outcome);
  rerenderActionAreas();
  refreshState();
  if (ui.detailId) fetchDetail();
}

// -------------------------------------------------------------------------------------- lanes

function laneRank(lane) {
  if (lane.state === "running") return 0;
  if (lane.state === "sleeping") return 1;
  if (LANE_ATTENTION.has(lane.state)) return 2;
  if (lane.hold) return 3;
  if (lane.open_wps.length) return 4;
  return 5;
}

function renderLanes(snapshot) {
  const rowsById = new Map(snapshot.wps.map((row) => [row.id, row]));
  // Lanes with a supervisor, a hold or a state that needs the orchestrator are shown; the rest fold
  // into two groups, so the work packages stay within reach.
  const shown = [];
  const resting = [];
  const idle = [];
  for (const lane of snapshot.lanes) {
    if (lane.live || lane.hold || LANE_ATTENTION.has(lane.state)) shown.push(lane);
    else if (lane.open_wps.length) resting.push(lane);
    else idle.push(lane);
  }
  shown.sort((a, b) => laneRank(a) - laneRank(b) || a.name.localeCompare(b.name));
  const live = snapshot.lanes.filter((lane) => lane.live).length;
  $("lanes-count").textContent = `${live} live · ${snapshot.lanes.length} known`;
  const rows = shown.map((lane) => laneRow(lane, rowsById));
  const folds = [
    ["resting-lanes", "Open work, no supervisor", resting, (lane) => `${esc(lane.name)} <span class="dim">(${lane.open_wps.length})</span>`],
    ["idle-lanes", "Idle lanes", idle, (lane) => esc(lane.name)],
  ];
  for (const [key, label, lanes, summary] of folds) {
    if (!lanes.length) continue;
    const open = !(key in ui.collapsed ? ui.collapsed[key] : true);
    rows.push(`<tr class="group-row"><th colspan="5" scope="rowgroup"><button type="button" class="group-toggle" data-toggle-group="${key}" data-focus-key="toggle-${key}" aria-expanded="${open}">${chevron()} ${esc(label)} <span class="count">${lanes.length}</span></button></th></tr>`);
    lanes.sort((a, b) => a.name.localeCompare(b.name));
    if (open) rows.push(...lanes.map((lane) => laneRow(lane, rowsById)));
    else rows.push(`<tr class="idle-lanes"><td colspan="5">${lanes.map(summary).join(", ")}</td></tr>`);
  }
  $("lanes-body").innerHTML = rows.join("") || '<tr><td colspan="5" class="empty">No lanes yet. <strong>wpo lanes resume</strong> starts one supervisor per lane with runnable work.</td></tr>';
}

function laneRow(lane, rowsById) {
  const tags = [lanePill(lane.state)];
  if (lane.pool) tags.push('<span class="pill pool">pool</span>');
  let now;
  if (lane.current_wp) {
    const row = rowsById.get(lane.current_wp);
    const session = !row || !row.session ? ""
      : row.session.role === "verify" ? '<span class="sub">verifying</span>'
      : `<span class="sub">${esc(row.session.role)} on <span class="mono">${esc(row.session.route)}</span></span>`;
    now = `<span class="wp-title">${wpButton(lane.current_wp)} <span title="${esc(row ? row.title : "")}">${esc(row ? row.title : "")}</span></span>${session}`;
  } else if (lane.sleep_until) {
    now = `<span class="wp-title">sleeping until <strong>${esc(clock(lane.sleep_until))}</strong> <span class="reason">· ${esc(lane.sleep_reason || "")}</span></span>`;
  } else {
    now = `<span class="wp-title reason" title="${esc(lane.last_line || "")}">${esc(lane.last_line || "")}</span>`;
  }
  if (lane.hold) now += `<span class="sub wraps"><span class="pill hold">held</span> ${esc(lane.hold)}</span>`;
  const open = lane.open_wps.length
    ? `<span class="lane-wps" title="${esc(lane.open_wps.join(", "))}">${lane.open_wps.length} · ${esc(lane.open_wps.slice(0, 2).join(", "))}${lane.open_wps.length > 2 ? " …" : ""}</span>`
    : '<span class="dim">—</span>';
  return `<tr class="lane-row" role="row">
    <td data-label="Lane" role="cell"><span class="lane-name">${esc(lane.name)}</span></td>
    <td data-label="Supervisor" role="cell" class="nowrap">${tags.join(" ")}</td>
    <td data-label="Now" role="cell" class="lane-now">${now}</td>
    <td data-label="Open" role="cell">${open}</td>
    <td data-label="Last line" role="cell" class="num nowrap" title="${esc(lane.last_line || "")}">${lane.last_line_at ? timeTag(lane.last_line_at) : '<span class="dim">—</span>'}</td>
  </tr>`;
}

// ------------------------------------------------------------------------------ work packages

function groupOf(row) {
  if (row.active) return "working";
  if (row.status !== "todo") return "inflight";
  return row.unmerged_deps.length ? "waiting" : "ready";
}

const GROUPS = [
  ["working", "Working now"],
  ["inflight", "In flight, not running"],
  ["ready", "Ready to start"],
  ["waiting", "Waiting on dependencies"],
];

function matchesFilter(row, text) {
  if (!text) return true;
  const haystack = `${row.id} ${row.title} ${row.lane} ${row.status} ${STATUS_LABEL[row.status] || ""} ${row.milestone}`.toLowerCase();
  return text.split(/\s+/).every((word) => haystack.includes(word));
}

function renderWorkPackages(snapshot, changed) {
  const text = ui.filterText.trim().toLowerCase();
  const rows = snapshot.wps.filter((row) => matchesFilter(row, text));
  const groups = new Map(GROUPS.map(([key]) => [key, []]));
  for (const row of rows) groups.get(groupOf(row)).push(row);
  groups.get("working").sort((a, b) => (b.last_activity || 0) - (a.last_activity || 0));
  groups.get("inflight").sort((a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) || (b.last_activity || 0) - (a.last_activity || 0));
  groups.get("ready").sort((a, b) => a.id.localeCompare(b.id));
  groups.get("waiting").sort((a, b) => a.id.localeCompare(b.id));
  const working = groups.get("working").length;
  $("wps-count").textContent = `${snapshot.wps.length} · ${working} working`;
  const html = [];
  for (const [key, label] of GROUPS) {
    const members = groups.get(key);
    if (!members.length) continue;
    const open = text ? true : !ui.collapsed[key];
    html.push(`<tr class="group-row"><th colspan="8" scope="rowgroup"><button type="button" class="group-toggle" data-toggle-group="${key}" data-focus-key="toggle-${key}" aria-expanded="${open}">${chevron()} ${esc(label)} <span class="count">${members.length}</span></button></th></tr>`);
    if (open) html.push(...members.map((row) => wpRow(row, changed.has(row.id))));
  }
  if (!html.length) {
    html.push(text
      ? `<tr><td colspan="8" class="empty">No open work package matches “${esc(ui.filterText)}”.</td></tr>`
      : '<tr><td colspan="8" class="empty"><strong>No open work packages.</strong> Draft WP files in the project\'s WP directory, commit them to main, then <strong>wpo lanes resume</strong>.</td></tr>');
  }
  $("wps-body").innerHTML = html.join("");
}

function wpRow(row, changed) {
  const classes = ["row"];
  if (changed) classes.push("changed");
  if (row.id === ui.detailId) classes.push("is-selected");
  const progress = row.latest_progress ? progressLine(row.latest_progress) : '<span class="dim">no progress logged yet</span>';
  let waiting = "";
  if (row.unmerged_deps.length) waiting = `<span class="waiting">after <span class="mono">${esc(row.unmerged_deps.join(", "))}</span></span>`;
  else if (row.route_wait_until && row.route_wait_until > nowSeconds()) waiting = `<span class="waiting">waits for a route until ${esc(clock(row.route_wait_until))}</span>`;
  else if (row.escalated_to) waiting = `<span class="waiting">escalated to <span class="mono">${esc(row.escalated_to)}</span></span>`;
  const findingsTitle = row.open_findings.map((finding) => `${finding.id}${finding.answered ? " (answered)" : ""} ${finding.location}`).join("\n");
  const unanswered = row.open_findings.filter((finding) => !finding.answered).length;
  return `<tr class="${classes.join(" ")}" role="row" data-row-wp="${esc(row.id)}">
    <td data-label="WP · lane" role="cell">${wpButton(row.id)}<span class="sub">${esc(row.lane || "no lane")}</span></td>
    <td data-label="Work package · latest progress" role="cell" class="title-cell"><span class="wp-title" title="${esc(row.title)}">${esc(row.title)}</span><span class="sub" title="${esc(row.latest_progress)}">${progress}</span></td>
    <td data-label="Status" role="cell" class="status-cell">${statusPill(row.status)}${waiting}</td>
    <td data-label="Steps" role="cell">${steps(row.steps_done, row.steps_total)}</td>
    <td data-label="Rounds" role="cell" class="rounds" title="${row.rounds} rounds · ${row.failures} failures since the last re-evaluation · ${row.evaluations} re-evaluations${row.handoffs ? ` · ${row.handoffs} handoffs this round` : ""}">R${row.rounds} · <span class="${row.failures ? "hot" : ""}">F${row.failures}</span> · <span class="${row.evaluations ? "hot" : ""}">E${row.evaluations}</span></td>
    <td data-label="Session · context" role="cell" class="session">${sessionCell(row)}</td>
    <td data-label="Findings" role="cell" class="num" title="${esc(findingsTitle)}">${row.open_findings.length ? `<span class="findings-count has-open">${row.open_findings.length}</span>${unanswered !== row.open_findings.length ? `<span class="sub">${unanswered} unanswered</span>` : ""}` : '<span class="dim">0</span>'}</td>
    <td data-label="Active" role="cell" class="num nowrap">${row.last_activity ? timeTag(row.last_activity) : '<span class="dim">—</span>'}</td>
  </tr>`;
}

function progressLine(entry) {
  const match = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?:\[([^\]]*)\] )?(.*)$/.exec(entry);
  if (!match) return esc(entry);
  return `<span class="actor">${esc(match[2] || "")}</span> ${esc(match[3])}`;
}

function steps(done, total) {
  if (!total) return '<span class="dim">—</span>';
  const segments = total <= 14 ? Array.from({ length: total }, (_, index) => `<span class="${index < done ? "done" : ""}"></span>`).join("")
    : `<span class="done" style="flex:${done}"></span><span style="flex:${total - done}"></span>`;
  return `<div class="steps" title="${done} of ${total} steps ticked"><span class="steps-text">${done}/${total}</span><span class="steps-bar" aria-hidden="true">${segments}</span></div>`;
}

function sessionCell(row) {
  const session = row.session;
  if (!session) return '<span class="dim">no session yet</span>';
  const current = session.live || row.active;
  const dot = session.live ? '<span class="live-dot" title="session running"></span>' : '<span class="idle-dot" title="no session running"></span>';
  // Between sessions the runner verifies; the route shown would be the previous session's.
  const verifying = current && session.role === "verify";
  const role = verifying ? "verifying" : current ? esc(session.role) : `last ${esc(session.role)}`;
  const route = verifying ? "" : `<span class="route" title="${esc(session.route)}">${esc(session.route)}</span>`;
  let line = `<span class="session-line">${dot}<span class="${current ? "" : "dim"}">${role}</span> ${route}</span>`;
  if (nativeThreadLink(session.native_task?.link)) {
    line += `<span class="sub"><a href="${esc(nativeThreadLink(session.native_task.link))}">Open native task in T3</a></span>`;
  }
  if (session.context_tokens && session.handoff_tokens) {
    const share = session.context_tokens / session.handoff_tokens;
    const near = share >= 0.8;
    line += `<div class="context" title="Context of the latest turn: ${session.context_tokens.toLocaleString()} tokens; handoff at ${session.handoff_tokens.toLocaleString()}${session.compactions ? `; ${session.compactions} compactions` : ""}">
      <span class="meter" role="meter" aria-label="Context against the handoff point" aria-valuemin="0" aria-valuemax="${session.handoff_tokens}" aria-valuenow="${session.context_tokens}"><span class="meter-fill ${near ? "is-over" : ""}" style="width:${Math.min(100, share * 100)}%"></span></span>
      <span class="context-text ${near ? "is-near" : ""}">${tokens(session.context_tokens)} / ${tokens(session.handoff_tokens)}</span></div>`;
  } else if (session.context_tokens) {
    line += `<span class="sub">${tokens(session.context_tokens)} tokens of context</span>`;
  } else if (row.last_context_tokens) {
    line += `<span class="sub">last session reached ${tokens(row.last_context_tokens)}</span>`;
  }
  return line;
}

// ------------------------------------------------------------------------- milestones, merges

function renderMilestones(milestones) {
  const order = ["done", "review", "in_progress", "changes_requested", "blocked", "todo"];
  $("milestones").innerHTML = milestones.map((milestone) => {
    const segments = order.filter((status) => milestone.counts[status])
      .map((status) => `<span class="s-${status}" style="width:${(milestone.counts[status] / milestone.total) * 100}%" title="${milestone.counts[status]} ${esc(STATUS_LABEL[status])}"></span>`).join("");
    const label = order.filter((status) => milestone.counts[status]).map((status) => `${milestone.counts[status]} ${STATUS_LABEL[status]}`).join(", ");
    return `<li><span class="milestone-name">${esc(milestone.name)}</span><span class="stack" role="img" aria-label="${esc(`${milestone.name}: ${label}`)}">${segments}</span><span class="milestone-count"><strong>${milestone.counts.done}</strong> / ${milestone.total} done</span></li>`;
  }).join("") || '<li class="empty">No WP files on main yet.</li>';
}

function renderMerged(merged) {
  $("merged").innerHTML = merged.map((entry) => `<li>${wpButton(entry.id)}<span class="wp-title" title="${esc(entry.title)}">${esc(entry.title)}</span><span class="nowrap">${timeTag(entry.merged_at)} <span class="dim mono">${esc(entry.commit.slice(0, 7))}</span></span></li>`).join("")
    || '<li class="empty">Nothing merged yet.</li>';
}

// -------------------------------------------------------------------------------------- events

function receiveEvents(events, reset) {
  const fresh = [];
  for (const event of events) {
    if (ui.eventIndexes.has(event.index)) continue;
    ui.eventIndexes.add(event.index);
    ui.events.push(event);
    if (!reset) fresh.push(event);
  }
  ui.events.sort((a, b) => a.index - b.index);
  if (ui.events.length > FEED_LIMIT) {
    for (const dropped of ui.events.splice(0, ui.events.length - FEED_LIMIT)) ui.eventIndexes.delete(dropped.index);
  }
  renderEvents(new Set(fresh.map((event) => event.index)));
  const attention = fresh.filter((event) => event.severity === "attention");
  if (attention.length) {
    $("announcer").textContent = attention.map((event) => `Needs you: ${event.kind}. ${event.message}`).join(" ");
  }
}

function eventSubject(event) {
  if (event.wp) return `<span class="event-subject">${wpButton(event.wp)}</span>`;
  const subject = event.supervisor ? `lane ${event.supervisor}` : event.job ? `job ${event.job}` : event.dispatch ? `dispatch ${event.dispatch}` : "";
  return subject ? `<span class="event-subject">${esc(subject)}</span>` : "";
}

function renderEvents(freshIndexes) {
  const list = $("events");
  const expanded = new Set([...list.querySelectorAll(".event.is-expanded")].map((item) => item.dataset.index));
  const shown = ui.events.filter((event) => ui.feedFilter === "all" || event.severity === "attention").slice().reverse();
  const html = shown.map((event) => {
    const attention = event.severity === "attention";
    const classes = ["event", attention ? "is-attention" : "", freshIndexes.has(event.index) ? "is-new" : "", expanded.has(String(event.index)) ? "is-expanded" : ""].filter(Boolean).join(" ");
    const when = eventTime(event);
    return `<li class="${classes}" data-index="${event.index}">
      <div class="event-head">${attention ? '<span class="event-flag" aria-label="needs you">!</span>' : ""}<span class="event-time" title="${esc(event.time)}">${esc(clock(when))}</span><span class="event-kind">${esc(event.kind)}</span>${eventSubject(event)}</div>
      <button type="button" class="event-message" aria-expanded="${expanded.has(String(event.index))}" data-focus-key="event-${event.index}" title="Expand or collapse event">${esc(event.message)}</button>
    </li>`;
  }).join("") || `<li class="empty">${ui.feedFilter === "attention" ? "Nothing in the journal needs you." : "The journal is empty."}</li>`;
  keepFocus(() => { list.innerHTML = html; });
}

// -------------------------------------------------------------------------------------- detail

function openDetail(id, pushHash = true) {
  if (pushHash && location.hash !== `#/wp/${id}`) history.pushState(null, "", `#/wp/${id}`);
  const opening = ui.detailId !== id;
  ui.detailId = id;
  ui.detail = null;
  ui.detailToggles = new Map();
  const panel = $("detail");
  panel.hidden = false;
  if (opening) {
    $("detail-title").innerHTML = `<span class="mono">${esc(id)}</span>`;
    $("detail-body").innerHTML = '<span class="skeleton"></span><span class="skeleton" style="width:40%"></span>';
  }
  $("detail-title").focus({ preventScroll: true });
  markSelected();
  renderDetailActions();
  fetchDetail();
}

function closeDetail() {
  if (!ui.detailId) return;
  const id = ui.detailId;
  ui.detailId = null;
  ui.detail = null;
  $("detail").hidden = true;
  renderDetailActions();
  if (location.hash.startsWith("#/wp/")) history.pushState(null, "", location.pathname);
  markSelected();
  document.querySelector(`#wps-body [data-open-wp="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
}

function markSelected() {
  for (const row of document.querySelectorAll("#wps-body tr.row")) row.classList.toggle("is-selected", row.dataset.rowWp === ui.detailId);
}

async function fetchDetail() {
  const id = ui.detailId;
  if (!id) return;
  ui.detailFetchedAt = Date.now();
  try {
    const response = await fetch(`/api/wp/${encodeURIComponent(id)}`, { cache: "no-store" });
    if (id !== ui.detailId) return;
    if (!response.ok) {
      $("detail-body").innerHTML = `<p class="notice warn">${esc(await response.text())}</p>`;
      return;
    }
    ui.detail = await response.json();
    const row = ui.snapshot && ui.snapshot.wps.find((candidate) => candidate.id === id);
    ui.detailSignature = row ? `${rowSignature(row)}|${row.last_activity}` : "";
    renderDetail();
  } catch (error) {
    if (id === ui.detailId) $("detail-body").innerHTML = `<p class="notice warn">Could not load ${esc(id)}: ${esc(error.message)}</p>`;
  }
}

function refreshDetailIfChanged() {
  const row = ui.snapshot.wps.find((candidate) => candidate.id === ui.detailId);
  const signature = row ? `${rowSignature(row)}|${row.last_activity}` : "";
  if (signature !== ui.detailSignature && Date.now() - ui.detailFetchedAt > DETAIL_REFRESH_MS) fetchDetail();
}

function renderDetail() {
  const detail = ui.detail;
  const body = $("detail-body");
  const scroll = body.scrollTop;
  for (const element of body.querySelectorAll("details[data-key]")) ui.detailToggles.set(element.dataset.key, element.open);
  const row = ui.snapshot && ui.snapshot.wps.find((candidate) => candidate.id === detail.id);
  $("detail-title").innerHTML = `<span class="mono">${esc(detail.id)}</span>${esc(detail.title)}`;
  body.innerHTML = [detailFacts(detail, row), detailNow(detail, row), detailSteps(detail), detailProgress(detail), detailFindings(detail),
    detailRounds(detail), detailSections(detail), detailLogs(detail)].join("");
  body.scrollTop = scroll;
  renderDetailActions();
}

function prose(key, summary, text, openByDefault = false) {
  if (!text || !String(text).trim()) return "";
  const open = ui.detailToggles.has(key) ? ui.detailToggles.get(key) : openByDefault;
  return `<details class="prose" data-key="${esc(key)}"${open ? " open" : ""}><summary>${summary}</summary><div class="prose-text">${esc(text)}</div></details>`;
}

function detailFacts(detail, row) {
  const fields = detail.fields;
  const merged = new Set();
  const deps = Array.isArray(fields.depends_on) ? fields.depends_on : [];
  const unmerged = new Set(row ? row.unmerged_deps : []);
  for (const dep of deps) if (!unmerged.has(dep)) merged.add(dep);
  const depText = deps.length ? deps.map((dep) => `${wpButton(dep)} <span class="dim">${merged.has(dep) ? "merged" : "not merged"}</span>`).join("<br>") : '<span class="dim">none</span>';
  const facts = [
    ["Status", statusPill(fields.status)],
    ["Lane", esc(fields.lane || "—")],
    ["Milestone", esc(fields.milestone)],
    ["Builder", `<span class="mono">${esc(fields.builder)}</span>${detail.ledger.escalated_to ? ` → <span class="mono">${esc(detail.ledger.escalated_to)}</span>` : ""}`],
    ["Reviewer", `<span class="mono">${esc(fields.reviewer)}</span>`],
    ["Depends on", depText],
  ];
  if (fields.mode) facts.push(["Mode", esc(fields.mode)]);
  if (fields.delegate) facts.push(["Delegate", `<span class="mono">${esc(fields.delegate)}</span>`]);
  facts.push(["Worktree", detail.worktree ? `<span class="mono">${esc(detail.worktree)}</span>` : '<span class="dim">none (read from main)</span>']);
  return `<section><dl class="facts">${facts.map(([term, value]) => `<div><dt>${term}</dt><dd>${value}</dd></div>`).join("")}</dl></section>`;
}

function detailNow(detail, row) {
  const parts = [];
  const session = row && row.session;
  if (session) {
    const links = session.log ? ` · <a href="/view/${runPath(session.log)}" target="_blank" rel="noopener">view session</a> · <a href="/raw/${runPath(session.log)}?tail" target="_blank" rel="noopener">raw tail</a>` : "";
    const context = session.context_tokens ? ` · context ${session.context_tokens.toLocaleString()}${session.handoff_tokens ? ` of ${session.handoff_tokens.toLocaleString()} (handoff point)` : ""}` : "";
    parts.push(`<p class="notice">${session.live ? "Running" : row.active ? "Lane is running it" : "Last session"}: <strong>${esc(session.role)}</strong> on <span class="mono">${esc(session.route)}</span>${session.started ? `, started ${timeTag(session.started)} ago` : ""}${context}${links}</p>`);
  }
  if (detail.no_route && row && row.route_wait_until && row.route_wait_until > nowSeconds()) {
    parts.push(`<p class="notice warn">No route for its ${esc(detail.no_route.role || "")} session (<span class="mono">${esc(detail.no_route.spec || "")}</span>) until ${esc(clock(row.route_wait_until))}.</p>`);
    parts.push(prose("no-route", "Why no route", detail.no_route.detail));
  }
  const writing = detail.ledger.writing;
  if (writing && writing.why) parts.push(`<p class="notice warn">The last builder session was cut off: ${esc(writing.why)}</p>`);
  if (!parts.length) return "";
  return `<section><h3>Now</h3>${parts.join("")}</section>`;
}

function detailSteps(detail) {
  if (!detail.steps.length) return "";
  const done = detail.steps.filter((step) => step.done).length;
  return `<section><h3>Steps <span class="count">${done} of ${detail.steps.length}</span></h3><ul class="checklist">${detail.steps.map((step) => `<li class="${step.done ? "done" : ""}"><span class="box" aria-hidden="true"></span><span><span class="visually-hidden">${step.done ? "done: " : "open: "}</span>${esc(step.text)}</span></li>`).join("")}</ul></section>`;
}

function detailProgress(detail) {
  const entries = detail.progress.slice().reverse();
  const items = entries.map((entry) => `<li><time>${esc(entry.at)}</time><p>${entry.actor ? `<span class="actor">[${esc(entry.actor)}]</span>` : ""}${esc(entry.text)}</p></li>`).join("");
  return `<section><h3>Progress log <span class="count">${detail.progress.length} entries, newest first</span></h3>${items ? `<ol class="timeline">${items}</ol>` : '<p class="dim">Nothing logged yet.</p>'}</section>`;
}

function findingIsOpen(finding) {
  return finding.severity === "BLOCKING" && ["open", "STILL-OPEN", "VETO-REJECTED"].includes(finding.status);
}

function detailFindings(detail) {
  const ledger = detail.findings;
  if (!ledger.findings.length) return `<section><h3>Review findings</h3><p class="dim">${ledger.reviews ? `${ledger.reviews} reviews, no findings recorded.` : "No review yet."}</p></section>`;
  const sorted = ledger.findings.slice().sort((a, b) => Number(findingIsOpen(b)) - Number(findingIsOpen(a)) || b.review - a.review);
  const open = sorted.filter(findingIsOpen).length;
  const items = sorted.map((finding) => {
    const isOpen = findingIsOpen(finding);
    const answer = finding.answer ? `<div class="finding-answer"><strong>Builder ${esc(finding.answer.kind)}${finding.answer.ground ? ` (${esc(finding.answer.ground)})` : ""}</strong> ${esc(finding.answer.at || "")}: ${esc(finding.answer.text)}</div>` : "";
    const rulings = (finding.rulings || []).map((ruling) => `<div class="finding-ruling"><strong>Review ${esc(ruling.review)}: ${esc(ruling.ruling)}</strong> ${esc(ruling.why)}</div>`).join("");
    const firstLine = String(finding.text || "").split("\n")[0];
    return `<div class="finding${isOpen ? " is-open" : ""}">
      <div class="finding-head"><span class="finding-id">${esc(finding.id)}</span><span class="pill ${finding.severity === "BLOCKING" ? "s-changes_requested" : "s-todo"}">${esc(finding.severity.toLowerCase())}</span><span class="pill plain ${isOpen ? "s-blocked" : "s-done"}">${esc(finding.status)}</span><span class="finding-location">${esc(finding.location)}</span></div>
      ${prose(`finding-${finding.id}`, esc(firstLine.slice(0, 160)), finding.text, isOpen)}
      ${answer}${rulings}
    </div>`;
  }).join("");
  return `<section><h3>Review findings <span class="count">${open} open of ${ledger.findings.length}, ${ledger.reviews} reviews</span></h3>${items}</section>`;
}

function detailRounds(detail) {
  const ledger = detail.ledger;
  const facts = [
    ["Rounds", ledger.rounds], ["Failures since re-evaluation", ledger.failures], ["Re-evaluations", ledger.evaluations],
    ["Cut-offs in a row", ledger.interruptions], ["Handoffs this round", ledger.handoffs], ["Round open", ledger.open ? "yes" : "no"],
  ];
  const history = (ledger.history || []).slice().reverse().map((entry, index) => `<li><div class="history-head"><span class="mono">${esc(entry.at)}</span><strong>round ${esc(entry.round)}</strong><span>${esc(entry.kind)}</span></div>${prose(`history-${entry.at}-${index}`, "feedback", entry.feedback)}</li>`).join("");
  const last = ledger.last_builder;
  return `<section><h3>Rounds</h3>
    <dl class="facts">${facts.map(([term, value]) => `<div><dt>${esc(term)}</dt><dd>${esc(value ?? "—")}</dd></div>`).join("")}</dl>
    ${last ? `<p class="dim">Last recorded builder session: <span class="mono">${esc(last.route)}</span>, ${tokens(last.context_tokens)} tokens of context${last.fresh_reason ? `; next starts fresh: ${esc(last.fresh_reason)}` : ""}.</p>` : ""}
    ${history ? `<ol class="history">${history}</ol>` : '<p class="dim">No failed round recorded.</p>'}
    ${prose("feedback", "Feedback the next builder gets", ledger.feedback)}
    ${prose("guidance", "Latest re-evaluation guidance", ledger.guidance)}
    ${prose("handoff", `Stored handoff${ledger.handoff && ledger.handoff.why ? ` (${esc(ledger.handoff.why)})` : ""}`, ledger.handoff && ledger.handoff.text)}
  </section>`;
}

function detailSections(detail) {
  const sections = ["Re-evaluation", "Review", "Handoff", "Try it", "Goal"]
    .map((heading) => prose(`section-${heading}`, esc(heading), detail.sections[heading])).filter(Boolean).join("");
  return sections ? `<section><h3>WP file</h3><p class="dim mono">${esc(detail.file)}</p>${sections}</section>` : "";
}

function detailLogs(detail) {
  if (!detail.logs.length) return '<section><h3>Logs</h3><p class="dim">No run directory yet.</p></section>';
  const rows = detail.logs.slice(0, 80).map((log) => {
    const what = log.session ? `${esc(log.role)} · <span class="mono">${esc(log.route)}</span>` : '<span class="dim">record</span>';
    const view = log.name.endsWith(".log") ? `<a href="/view/${runPath(log.path)}" target="_blank" rel="noopener">view</a>` : "";
    const tail = log.size > 1024 * 1024 ? `<a href="/raw/${runPath(log.path)}?tail" target="_blank" rel="noopener">tail</a>` : "";
    return `<tr><td class="name">${esc(log.name)}</td><td>${what}</td><td class="num">${bytes(log.size)}</td><td class="num">${timeTag(log.modified)}</td><td class="links">${view}<a href="/raw/${runPath(log.path)}" target="_blank" rel="noopener">raw</a>${tail}</td></tr>`;
  }).join("");
  return `<section><h3>Logs <span class="count">${detail.logs.length} files, newest first</span></h3><div class="table-wrap" tabindex="0" role="region" aria-label="Run logs"><table class="logs"><thead><tr><th scope="col">File</th><th scope="col">Session</th><th scope="col" class="num">Size</th><th scope="col" class="num">Written</th><th scope="col">Open</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

// -------------------------------------------------------------------------------- interaction

document.addEventListener("click", (event) => {
  if (event.target.closest('a[href="#needs-you"]')) $("needs-disclosure").open = true;
  const opener = event.target.closest("[data-open-wp]");
  if (opener) { openDetail(opener.dataset.openWp); return; }
  const toggle = event.target.closest("[data-toggle-group]");
  if (toggle) {
    const key = toggle.dataset.toggleGroup;
    ui.collapsed[key] = toggle.getAttribute("aria-expanded") === "true";
    saveCollapsed();
    keepFocus(() => { renderLanes(ui.snapshot); renderWorkPackages(ui.snapshot, new Set()); });
    return;
  }
  const filter = event.target.closest("[data-feed-filter]");
  if (filter) {
    ui.feedFilter = filter.dataset.feedFilter;
    localStorage.setItem("wpo.feedFilter", ui.feedFilter);
    for (const button of document.querySelectorAll("[data-feed-filter]")) button.setAttribute("aria-pressed", String(button === filter));
    renderEvents(new Set());
    return;
  }
  const message = event.target.closest(".event-message");
  if (message) {
    const expanded = message.closest(".event").classList.toggle("is-expanded");
    message.setAttribute("aria-expanded", String(expanded));
    return;
  }
  const row = event.target.closest("tr.row[data-row-wp]");
  if (row && !event.target.closest("a, button") && !window.getSelection().toString()) openDetail(row.dataset.rowWp);
});

$("detail-close").addEventListener("click", closeDetail);
const TYPING_ELEMENTS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
document.addEventListener("keydown", (event) => {
  const typing = TYPING_ELEMENTS.has(document.activeElement.tagName);
  // Escape in a form leaves the text alone; it closes the detail only from elsewhere.
  if (event.key === "Escape" && ui.detailId && !typing && !document.querySelector(":popover-open")) closeDetail();
  if (event.key === "/" && !typing) { event.preventDefault(); $("wp-filter").focus(); }
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form.action-form");
  if (!form) return;
  event.preventDefault();
  submitAction(form, event.submitter);
});

// Typed text and choices survive a re-render of their form until they are sent.
function keepDraft(event) {
  const field = event.target.closest("[data-draft]");
  if (field) ui.drafts.set(field.dataset.draft, field.value);
}
document.addEventListener("input", keepDraft);
document.addEventListener("change", keepDraft);

$("wp-filter").addEventListener("input", (event) => {
  ui.filterText = event.target.value;
  if (ui.snapshot) renderWorkPackages(ui.snapshot, new Set());
});

function routeFromHash() {
  const match = /^#\/wp\/([\w.-]+)$/.exec(location.hash);
  if (match) openDetail(match[1], false);
  else if (ui.detailId) closeDetail();
}
window.addEventListener("popstate", routeFromHash);

// Ages tick on the client; the snapshot holds absolute times only.
setInterval(() => {
  for (const element of document.querySelectorAll("time[data-ago]")) {
    const epoch = Number(element.dataset.ago);
    if (epoch) element.textContent = ago(epoch);
  }
  renderConnection();
}, 10000);

for (const button of document.querySelectorAll("[data-feed-filter]")) button.setAttribute("aria-pressed", String(button.dataset.feedFilter === ui.feedFilter));
renderRequestBox();
connect();
routeFromHash();
