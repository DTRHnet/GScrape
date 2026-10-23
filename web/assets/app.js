const state = {
  view: "overview",
  runs: [],
  profiles: [],
  accounts: [],
  coreAccounts: [],
  rawExports: [],
  expandedRawExports: new Set(),
  rawMessages: {},
  rawMessageDetails: {},
  debugConsole: [],
  logging: { config: { level: "INFO", max_bytes: 5242880, backup_count: 5 }, files: [], tail: [] },
  analysis: { total_matches: 0, unique_message_ids: 0, high_score_count: 0, category_counts: {} },
  debug: null,
  detail: null,
  selectedRuns: new Set(),
  disclosures: {
    matchAnalysis: false,
    recentActivity: true,
  },
};

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
}[char]));

async function api(path, options = {}) {
  const started = performance.now();
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload;
  try { payload = await response.json(); } catch (_) { payload = { error: "Invalid JSON response" }; }
  const networkEntry = { type: "network", message: `${options.method || "GET"} ${path} → ${response.status}`, method: options.method || "GET", path, status: response.status, duration_ms: Math.round(performance.now() - started), time: new Date().toISOString() };
  state.debugConsole.unshift(networkEntry); state.debugConsole = state.debugConsole.slice(0, 100);
  if (!response.ok) { recordDebug("error", `${options.method || "GET"} ${path} returned ${response.status}: ${payload.error || "Request failed"}`); throw Error(payload.error || "Request failed"); }
  return payload;
}

function recordDebug(type, message, detail = "") {
  state.debugConsole.unshift({ type, message: String(message), detail: String(detail || ""), time: new Date().toISOString() });
  state.debugConsole = state.debugConsole.slice(0, 100);
  if (state.view === "debug") render();
}

window.addEventListener("error", (event) => recordDebug("error", event.message || "Unhandled browser error", `${event.filename || ""}:${event.lineno || ""}`));
window.addEventListener("unhandledrejection", (event) => recordDebug("error", "Unhandled promise rejection", event.reason?.stack || event.reason || ""));
document.addEventListener("securitypolicyviolation", (event) => recordDebug("csp", `CSP blocked ${event.blockedURI || event.violatedDirective}`, event.violatedDirective || ""));

function shell(content) { $("#app").innerHTML = content; }
function stat(label, value, sub) {
  return `<div class="stat-card"><div class="stat-label">${label}</div><div class="stat">${value}</div><div class="muted">${sub}</div></div>`;
}

function statusClass(status) {
  return {
    completed: "green",
    failed: "red",
    running: "blue",
    queued: "amber",
    cancelled: "red",
  }[status] || "blue";
}

function rawExportDetail(item) {
  const filters = state.rawMessages[item.id]?.filters || {};
  const processed = Number(item.processed_messages || 0);
  const saved = Number(item.message_count || 0);
  const failures = Number(item.failure_count || 0);
  const pages = Number(item.pages_scanned || 0);
  const estimate = item.total_messages == null ? null : Number(item.total_messages);
  const usableEstimate = estimate != null && estimate > 0 && estimate >= processed;
  const percent = usableEstimate ? Math.min(100, Math.round((processed / estimate) * 100)) : null;
  const progressLabel = estimate == null
    ? `${processed} messages processed`
    : usableEstimate
      ? `${processed} messages processed · Gmail estimate ${estimate}`
      : `${processed} messages processed · Gmail estimate ${estimate} (estimate exceeded)`;
  return `<tr class="raw-export-detail-row"><td colspan="6"><div class="raw-export-detail">
    <div class="progress-head"><strong>Export progress</strong><span>${esc(progressLabel)}${percent == null ? "" : ` · ${percent}%`}</span></div>
    <div class="progress-track" role="progressbar" aria-label="Raw export progress relative to Gmail estimate" aria-valuemin="0" aria-valuemax="${usableEstimate ? estimate : 0}" aria-valuenow="${processed}"><span class="progress-fill progress-${percent == null ? 0 : Math.min(100, Math.floor(percent / 10) * 10)}"></span></div>
    <div class="raw-export-metrics"><span><strong>Messages processed</strong>${processed}</span><span><strong>Messages saved</strong>${saved}</span><span><strong>Failures</strong>${failures}</span><span><strong>Pages scanned</strong>${pages}</span><span><strong>Gmail estimate</strong>${estimate == null ? "Not reported" : estimate}</span><span><strong>Attachments</strong>${item.attachment_count || 0}</span></div>
    <div class="muted">Created ${esc(item.created_at || "unknown")} · Updated ${esc(item.updated_at || "unknown")}${item.completed_at ? ` · Completed ${esc(item.completed_at)}` : ""}</div>
    ${item.last_error ? `<div class="raw-export-error"><strong>Last error:</strong> ${esc(item.last_error)}</div>` : ""}
    <div class="section-head raw-messages-head"><div><strong>Stored messages</strong><span class="muted">Browse the local raw database without re-querying Gmail.</span></div><button class="button ghost compact" type="button" data-raw-messages="${esc(item.id)}">Load messages</button></div>
    <form class="raw-message-filters" data-raw-message-filters="${esc(item.id)}"><label>Title / subject<input name="subject" maxlength="200" value="${esc(filters.subject || "")}" placeholder="Search subject"></label><label>Sender<input name="sender" maxlength="200" value="${esc(filters.sender || "")}" placeholder="Name or email"></label><label>Attachment type<select name="attachment_type"><option value="">Any attachment</option><option value="image" ${filters.attachment_type === "image" ? "selected" : ""}>Images</option><option value="document" ${filters.attachment_type === "document" ? "selected" : ""}>Documents</option><option value="spreadsheet" ${filters.attachment_type === "spreadsheet" ? "selected" : ""}>Spreadsheets</option><option value="archive" ${filters.attachment_type === "archive" ? "selected" : ""}>Archives</option><option value="audio" ${filters.attachment_type === "audio" ? "selected" : ""}>Audio</option><option value="video" ${filters.attachment_type === "video" ? "selected" : ""}>Video</option><option value="other" ${filters.attachment_type === "other" ? "selected" : ""}>Other files</option></select></label><button class="button compact" type="submit">Apply filters</button><button class="button ghost compact" type="button" data-raw-filter-clear="${esc(item.id)}">Clear</button></form>
    <div data-raw-message-list="${esc(item.id)}">${state.rawMessages[item.id] ? rawMessageRows(item.id) : '<span class="muted">Select Load messages to inspect saved records.</span>'}</div>
  </div></td></tr>`;
}

function rawMessageRows(exportId) {
  const payload = state.rawMessages[exportId];
  if (!payload) return "";
  const rows = (payload.messages || []).map((message) => {
    const detail = state.rawMessageDetails[`${exportId}:${message.id}`];
    const from = formatAddresses(message.from);
    const to = formatAddresses(message.to);
    const date = message.date || (message.internal_date ? new Date(Number(message.internal_date)).toLocaleString() : "Unknown date");
    const subject = message.subject || "(no subject)";
    return `<div class="raw-message-item"><div class="raw-message-summary"><div><h4>${esc(subject)}</h4><div><strong>From:</strong> ${esc(from || "Unknown sender")}</div><div><strong>To:</strong> ${esc(to || "Unknown recipient")}</div><span class="muted">${esc(date)} · ${message.attachment_count || 0} attachment(s) · Header risk: ${esc(message.header_risk || "unknown")}</span><small class="muted">Provider ID: ${esc(message.provider_message_id)}</small></div><div class="actions"><button class="button ghost compact" type="button" data-raw-message-detail="${esc(exportId)}" data-raw-message-id="${message.id}">${detail ? "Hide details" : "View details"}</button><a class="button ghost compact" href="/api/raw-exports/${encodeURIComponent(exportId)}/messages/${message.id}/eml">Download EML</a></div></div>${detail ? rawMessageDetail(exportId, detail) : ""}</div>`;
  }).join("");
  const next = payload.offset + payload.limit < payload.total ? `<button class="button ghost compact" type="button" data-raw-messages-next="${esc(exportId)}">Next page</button>` : "";
  const previous = payload.offset > 0 ? `<button class="button ghost compact" type="button" data-raw-messages-prev="${esc(exportId)}">Previous page</button>` : "";
  return `<div class="raw-message-list">${rows || '<span class="muted">No messages saved for this export.</span>'}</div><div class="actions raw-message-pagination"><span class="muted">${payload.total ? payload.offset + 1 : 0}–${Math.min(payload.offset + payload.limit, payload.total)} of ${payload.total}</span>${previous}${next}</div>`;
}

function formatAddresses(addresses) {
  return (addresses || []).map((item) => item.name ? `${item.name} <${item.address}>` : item.address).join(", ");
}

function rawMessageDetail(exportId, message) {
  const analysis = message.header_analysis || {};
  const fields = analysis.fields || {};
  const findings = (analysis.findings || []).map((finding) => `<li><strong>${esc(finding.severity)} · ${esc(finding.title)}</strong><span class="muted">${esc(finding.detail)}</span></li>`).join("");
  const auth = Object.entries(analysis.authentication || {}).map(([name, value]) => `<span class="tag">${esc(name.toUpperCase())}: <strong>${esc(value)}</strong></span>`).join("") || '<span class="muted">No Authentication-Results values parsed.</span>';
  const hops = (analysis.received_hops || []).map((hop) => `<li><strong>Hop ${hop.hop}</strong> ${esc(hop.ips?.join(", ") || "no IP parsed")}<div class="muted">${esc(hop.raw)}</div></li>`).join("");
  const attachments = (message.attachments || []).map((attachment) => {
    const url = `/api/raw-exports/${encodeURIComponent(exportId)}/messages/${message.id}/attachment/${attachment.id}`;
    const action = attachment.download_status === "downloaded" ? `<a class="button ghost compact" href="${url}">Download</a>` : `<span class="muted">${esc(attachment.download_status || "metadata-only")}</span>`;
    return `<li><code>${esc(attachment.filename)}</code> · ${esc(attachment.mime_type)} · ${esc(attachment.size_bytes)} bytes ${action}</li>`;
  }).join("");
  return `<div class="raw-message-detail"><div class="actions"><a class="button ghost compact" href="/api/raw-exports/${encodeURIComponent(exportId)}/messages/${message.id}/eml?inline=1" target="_blank" rel="noopener">Open RFC822</a><span class="badge ${analysis.risk_level === "high" ? "red" : analysis.risk_level === "medium" ? "amber" : "green"}">Header risk: ${esc(analysis.risk_level || "unknown")} · ${esc(analysis.score ?? 0)}/100</span></div><div class="raw-header-summary"><div><strong>Subject</strong><span>${esc(fields.subject || "(no subject)")}</span></div><div><strong>From</strong><span>${esc(formatAddresses(fields.from) || "Unknown")}</span></div><div><strong>To</strong><span>${esc(formatAddresses(fields.to) || "Unknown")}</span></div><div><strong>Reply-To</strong><span>${esc(formatAddresses(fields.reply_to) || "Not set")}</span></div><div><strong>Return-Path</strong><span>${esc(formatAddresses(fields.return_path) || "Not set")}</span></div><div><strong>Date</strong><span>${esc(fields.date || "Unknown")}</span></div></div><p class="muted">Thread ${esc(message.thread_id || "unknown")} · Provider ID ${esc(message.provider_message_id)} · Stored ${esc(message.created_at || "unknown")}</p><h4>Authentication</h4><div class="tags">${auth}</div><h4>Header findings</h4><ul>${findings || '<li class="muted">No concerning header indicators detected by the local analyzer.</li>'}</ul><h4>Delivery path</h4><ol>${hops || '<li class="muted">No Received headers available.</li>'}</ol><h4>Attachments</h4><ul>${attachments || '<li class="muted">No attachment metadata.</li>'}</ul><details><summary>Raw Gmail API JSON</summary><pre>${esc(JSON.stringify(message.raw_api, null, 2))}</pre></details></div>`;
}

async function loadRawMessages(exportId, offset = 0) {
  try {
    const filter = state.rawMessages[exportId]?.filters || {};
    const params = new URLSearchParams({ limit: "25", offset: String(offset), subject: filter.subject || "", sender: filter.sender || "", attachment_type: filter.attachment_type || "" });
    const payload = await api(`/api/raw-exports/${encodeURIComponent(exportId)}/messages?${params}`);
    state.rawMessages[exportId] = payload;
    render();
  } catch (error) { alert(error.message); }
}

function applyRawMessageFilters(event) {
  event.preventDefault();
  const form = event.target;
  const exportId = form.dataset.rawMessageFilters;
  state.rawMessages[exportId] = { ...(state.rawMessages[exportId] || {}), filters: Object.fromEntries(new FormData(form)), offset: 0 };
  loadRawMessages(exportId, 0);
}

async function loadRawMessageDetail(exportId, rowId) {
  const key = `${exportId}:${rowId}`;
  if (state.rawMessageDetails[key]) { delete state.rawMessageDetails[key]; render(); return; }
  try {
    const payload = await api(`/api/raw-exports/${encodeURIComponent(exportId)}/messages/${rowId}`);
    state.rawMessageDetails[key] = payload.message;
    render();
  } catch (error) { alert(error.message); }
}

function coreJobRows(jobs) {
  return (jobs || []).map((job) => `<tr><td><code>${esc(job.id)}</code></td><td>${esc(job.provider || "")}</td><td><span class="badge ${statusClass(job.status)}">${esc(job.status)}</span></td><td>${esc(job.created_at || "")}</td><td>${esc(job.error_code || "")}</td><td>${job.status === "queued" ? `<button class="button ghost compact danger" data-core-action="cancel" data-core-id="${esc(job.id)}">Cancel</button>` : ""}${["failed", "cancelled"].includes(job.status) ? `<button class="button ghost compact" data-core-action="retry" data-core-id="${esc(job.id)}">Retry</button>` : ""}</td></tr>`).join("");
}

async function refreshCoreJobs() {
  try {
    const payload = await api("/api/core/jobs");
    const node = document.querySelector("[data-core-jobs]");
    if (node) node.innerHTML = coreJobRows(payload.jobs);
  } catch (_) { /* legacy dashboard remains usable if the control plane is unavailable */ }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-core-action]");
  if (button) coreJobAction(button.dataset.coreId, button.dataset.coreAction).catch(() => {});
});

async function coreJobAction(id, action) {
  await api(`/api/core/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
  await refreshCoreJobs();
}

function selectedRunIds() {
  return [...state.selectedRuns].filter((id) => state.runs.some((run) => run.id === id));
}

function bulkActionBar() {
  const selected = selectedRunIds();
  if (!selected.length) return "";
  return `<div class="bulk-bar" role="toolbar" aria-label="Bulk actions">
    <div class="bulk-selection"><span class="selection-mark">✓</span><strong>${selected.length}</strong> run${selected.length === 1 ? "" : "s"} selected</div>
    <div class="bulk-actions">
      <button class="button ghost danger" type="button" data-bulk-action="delete">Delete selected</button>
      <span class="bulk-divider"></span>
      <span class="bulk-label">Merge &amp; export</span>
      <button class="button compact" type="button" data-bulk-action="markdown">Markdown</button>
      <button class="button compact" type="button" data-bulk-action="csv">CSV</button>
      <button class="button compact" type="button" data-bulk-action="json">JSON</button>
    </div>
  </div>`;
}

function attachmentCards(runId, match) {
  const items = match.acquired_attachments || [];
  if (!items.length) {
    const names = (match.attachment_names || []).slice(0, 8).map((name) => esc(name)).join(", ");
    return names ? `<div class="muted attachment-names">${names}</div>` : '<span class="muted">No acquired files</span>';
  }
  return `<div class="attachment-gallery">${items.map((item) => {
    const label = esc(item.filename || item.stored_name || "attachment");
    const meta = [
      item.size_bytes != null ? `${Number(item.size_bytes).toLocaleString()} bytes` : "",
      item.sha256 ? `sha256 ${String(item.sha256).slice(0, 12)}…` : "",
    ].filter(Boolean).join(" · ");
    const thumb = item.thumbnail_url
      ? `<a class="attachment-thumb" href="${esc(item.download_url || item.thumbnail_url)}" target="_blank" rel="noopener" aria-label="Open ${label}"><img src="${esc(item.thumbnail_url)}" alt="${label}" loading="lazy" decoding="async" onerror="this.closest('.attachment-thumb').classList.add('load-error');this.replaceWith(Object.assign(document.createElement('div'),{className:'attachment-thumb placeholder',textContent:'!' }));"></a>`
      : `<div class="attachment-thumb placeholder">${label.slice(0, 1).toUpperCase()}</div>`;
    const download = item.download_url
      ? `<a class="button ghost compact" href="${esc(item.download_url)}" download>Download</a>`
      : "";
    return `<article class="attachment-card"><label class="attachment-select"><input type="checkbox" class="attachment-checkbox" data-attachment-url="${esc(item.download_url || "")}" aria-label="Select ${label}"><span class="checkmark"></span></label>${thumb}<div class="attachment-info"><strong title="${label}">${label}</strong><div class="muted">${esc(meta)}</div><div class="row-actions">${download}${item.thumbnail_url ? `<a class="button ghost compact" href="${esc(item.thumbnail_url)}" target="_blank" rel="noopener" title="Open image">↗ <span>Expand</span></a>` : ""}</div></div></article>`;
  }).join("")}</div>`;
}

function findingRows(findings) {
  return findings.map((match) => `<tbody class="finding-group">
    <tr class="finding-images"><td colspan="5"><div class="finding-gallery">${attachmentCards("", match)}</div></td></tr>
    <tr class="finding-meta"><td><span class="meta-label">Date</span>${esc(match.message_date || "Unknown")}</td><td><span class="meta-label">Sender</span><span class="wrap-cell">${esc(match.sender || "Unknown")}</span></td><td><span class="meta-label">Subject / content</span><span class="wrap-cell">${esc(match.subject || "(no subject)")}<div class="muted">${esc((match.categories || []).join(", "))}</div></span></td><td><span class="meta-label">Evidence</span>${esc(match.attachment_names?.length ? `${match.attachment_names.length} attachment(s)` : "Message match")}</td><td><a class="button ghost compact" target="_blank" rel="noopener" href="${esc(match.message_location || "#")}">Open Gmail</a></td></tr>
  </tbody>`).join("");
}

function selectedAttachmentUrls() {
  return [...document.querySelectorAll(".attachment-checkbox:checked")].map((box) => box.dataset.attachmentUrl).filter(Boolean);
}

function downloadSelectedAttachments() {
  const urls = selectedAttachmentUrls();
  if (!urls.length) return;
  urls.forEach((url, index) => setTimeout(() => { const link = document.createElement("a"); link.href = url; link.download = ""; document.body.appendChild(link); link.click(); link.remove(); }, index * 120));
}

function toggleAllAttachments(checked) {
  document.querySelectorAll(".attachment-checkbox").forEach((box) => { box.checked = checked; });
}

function detailExpansion(run) {
  if (!state.detail?.run || state.detail.run.id !== run.id) return "";
  const detail = state.detail;
  const summary = detail.summary || {};
  const findings = detail.matches || [];
  const error = detail.run.error ? `<div class="run-error"><strong>Run issue</strong><span>${esc(detail.run.error)}</span></div>` : "";
  return `<div class="run-expansion">
    <div class="run-expansion-head"><div><span class="eyebrow section-kicker">RUN DETAIL · ${esc(run.id)}</span><h3>${esc(detail.note || "Redacted run details")}</h3></div><div class="actions"><a class="button ghost compact" href="/run/${encodeURIComponent(run.id)}" target="_blank" rel="noopener">Open full page ↗</a><button class="button ghost compact" type="button" data-run-action="details" data-run-id="${esc(run.id)}">Close</button></div></div>
    <div class="run-metrics">
      <div><span>Pages parsed</span><strong>${run.pages || 0}</strong></div>
      <div><span>Messages returned</span><strong>${run.listed || 0}</strong></div>
      <div><span>Messages fetched</span><strong>${run.scanned || 0}</strong></div>
      <div><span>Matches found</span><strong>${summary.total_matches ?? run.matches ?? 0}</strong></div>
    </div>
    ${error}
    <div class="run-expansion-section"><div class="section-head"><div><strong>Findings</strong><span class="muted">Date, sender, subject, and acquired files</span></div><div class="actions attachment-actions"><span class="select-all-control"><input id="select-all-attachments" type="checkbox" data-select-all-attachments><label for="select-all-attachments">Select all</label></span><button class="button ghost compact" type="button" data-download-selected>↓ <span>Download selected</span></button><a class="button ghost compact" href="/api/export?format=run-report&run_id=${encodeURIComponent(run.id)}">Export</a></div></div>
      ${findings.length ? `<div class="table-wrap mini-table"><table class="table findings-table"><thead><tr><th colspan="5">Email evidence · thumbnails above, sortable metadata below</th></tr></thead>${findingRows(findings)}</table></div>` : '<div class="empty compact-empty">No categorized findings recorded yet.</div>'}
    </div>
    <p class="muted run-files">Result folder: <code>${esc(detail.files?.results_dir || "")}</code><br>Scan log: <code>${esc(detail.files?.scan_log || "")}</code>${detail.files?.acquired_dir ? `<br>Acquired files: <code>${esc(detail.files.acquired_dir)}</code>` : ""}</p>
  </div>`;
}

function runRows({ idPrefix = "runs" } = {}) {
  if (!state.runs.length) return '<div class="card empty">No runs yet. Create your first search to begin.</div>';
  const selected = selectedRunIds();
  const allSelected = selected.length === state.runs.length;
  return `<div class="table-toolbar"><span class="select-all-control"><span class="checkbox-label"><input id="select-all-runs" type="checkbox" class="select-all-runs" ${allSelected ? "checked" : ""} aria-label="Select all runs"><span class="checkmark"></span></span><label for="select-all-runs">Select all</label></span><span class="muted">${state.runs.length} recorded run${state.runs.length === 1 ? "" : "s"}</span></div>
  <div class="run-list" data-run-list="${esc(idPrefix)}">${state.runs.map((run) => {
    const open = state.detail?.run?.id === run.id;
    return `<article class="run-item ${open ? "is-open" : ""}" data-run-id="${esc(run.id)}">
      <div class="run-row" data-run-toggle="${esc(run.id)}" role="button" tabindex="0" aria-expanded="${open}">
        <span class="checkbox-label run-select"><input type="checkbox" class="run-checkbox" data-run-id="${esc(run.id)}" ${selected.includes(run.id) ? "checked" : ""} aria-label="Select run ${esc(run.id)}"><span class="checkmark"></span></span>
        <div class="run-main">
          <div class="run-primary">
            <code class="run-id">${esc(run.id)}</code>
            <span class="badge ${statusClass(run.status)}"><span class="status-dot"></span>${esc(run.status)}</span>
            <span class="run-chevron" aria-hidden="true">›</span>
          </div>
          <div class="run-meta">
            <span>${esc(run.category || "attachments")}</span>
            <span class="run-meta-sep">·</span>
            <span class="run-query" title="${esc(run.query || "preset")}">${esc(run.query || "preset")}</span>
          </div>
          <div class="run-stats">
            <span><strong>${run.scanned || 0}</strong> scanned</span>
            <span><strong>${run.matches || 0}</strong> matches</span>
            <span>${esc(new Date(run.started_at).toLocaleString())}</span>
          </div>
        </div>
        <div class="run-actions">
          ${["queued", "running"].includes(run.status) ? `<button class="button ghost compact danger" type="button" data-run-action="stop" data-run-id="${esc(run.id)}">Stop</button>` : ""}
          ${run.status === "cancelled" ? `<button class="button ghost compact" type="button" data-run-action="resume" data-run-id="${esc(run.id)}">Resume</button>` : ""}
          ${["failed", "cancelled"].includes(run.status) ? `<button class="button ghost compact" type="button" data-run-action="retry" data-run-id="${esc(run.id)}">Retry</button>` : ""}
          <button class="button ghost compact danger" type="button" data-run-action="delete" data-run-id="${esc(run.id)}">Remove</button>
        </div>
      </div>
      ${detailExpansion(run)}
    </article>`;
  }).join("")}</div>`;
}

function disclosure(id, title, description, content, open = false, actions = "") {
  return `<section class="disclosure card section ${open ? "is-open" : ""}" data-disclosure-panel="${id}">
    <div class="disclosure-head"><button class="disclosure-trigger" type="button" data-disclosure="${id}" aria-expanded="${open}">
      <span class="disclosure-heading"><span class="chevron" aria-hidden="true">›</span><span><strong>${title}</strong>${description ? `<small>${description}</small>` : ""}</span></span>
    </button>${actions}</div>
    <div class="disclosure-body"><div class="disclosure-content">${content}</div></div>
  </section>`;
}

function categoryRows() {
  const counts = state.analysis.category_counts || {};
  return Object.entries(counts).map(([category, count]) =>
    `<span class="tag">${esc(category)} <strong>${count}</strong></span>`).join("") ||
    '<span class="muted">No analyzed matches yet.</span>';
}

async function load() {
  const [runs, profiles, accounts, analysis, logging, rawExports] = await Promise.all([
    api("/api/runs"), api("/api/profiles"), api("/api/accounts"), api("/api/analysis"), api("/api/logging"), api("/api/raw-exports"),
  ]);
  state.runs = runs.runs;
  state.profiles = profiles.profiles;
  state.accounts = accounts.accounts;
  state.analysis = analysis.summary;
  state.selectedRuns = new Set([...state.selectedRuns].filter((id) => state.runs.some((run) => run.id === id)));
  state.logging = logging;
  state.rawExports = rawExports.exports || [];
  render();
  refreshCoreJobs();
}

async function refreshAccounts() {
  const payload = await api("/api/accounts");
  state.accounts = payload.accounts;
  document.querySelectorAll("[data-account-status]").forEach((node) => {
    const account = state.accounts.find((item) => item.id === node.dataset.accountStatus);
    if (account) {
      node.textContent = account.status;
      node.className = `badge ${account.status === "connected" ? "green" : ""}`;
    }
  });
}

async function refreshCoreAccounts(shouldRender = false) {
  try {
    const payload = await api("/api/core/accounts");
    state.coreAccounts = payload.accounts || [];
    if (shouldRender && state.view === "settings") render();
  } catch (_) { state.coreAccounts = []; }
}

async function refreshRunState() {
  const [payload, analysis] = await Promise.all([api("/api/runs"), api("/api/analysis")]);
  const nextRuns = payload.runs || [];
  const runsChanged = JSON.stringify(nextRuns) !== JSON.stringify(state.runs);
  const analysisChanged = JSON.stringify(analysis.summary) !== JSON.stringify(state.analysis);
  state.runs = nextRuns;
  state.analysis = analysis.summary;
  let detailChanged = false;
  if (state.detail?.run?.id) {
    try {
      const nextDetail = await api(`/api/runs/${encodeURIComponent(state.detail.run.id)}`);
      detailChanged = JSON.stringify(nextDetail) !== JSON.stringify(state.detail);
      state.detail = nextDetail;
    }
    catch (_) { state.detail = null; }
  }
  if (runsChanged || analysisChanged || detailChanged) {
    if (state.view === "overview" || state.view === "history") render();
  }
}

async function refreshDebug() {
  state.debug = await api("/api/debug");
  if (state.view === "debug") render();
}

async function showRun(id) {
  if (state.detail?.run?.id === id) {
    state.detail = null;
    render();
    return;
  }
  try {
    state.detail = await api(`/api/runs/${encodeURIComponent(id)}`);
    render();
  } catch (error) { alert(error.message); }
}

async function loadFullPageRun(id) {
  try {
    state.detail = await api(`/api/runs/${encodeURIComponent(id)}`);
    render();
  } catch (error) {
    shell(`<div class="card empty"><h2>Run unavailable</h2><p class="muted">${esc(error.message)}</p><a class="button" href="/">Return to dashboard</a></div>`);
  }
}

async function deleteRun(id) {
  if (!confirm("Remove this run from dashboard history? The append-only audit log will be preserved.")) return;
  try {
    await api(`/api/runs/${encodeURIComponent(id)}/delete`, { method: "POST", body: "{}" });
    if (state.detail?.run.id === id) state.detail = null;
    await load();
    state.view = "history";
    render();
  } catch (error) { alert(error.message); }
}

async function changeRunState(id, action) {
  const labels = { stop: "Stop this running scan?", resume: "Resume this stopped scan?", retry: "Retry this scan?" };
  if (!confirm(labels[action])) return;
  try {
    await api(`/api/runs/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
    await load();
  } catch (error) { alert(error.message); }
}

async function deleteSelectedRuns() {
  const ids = selectedRunIds();
  if (!ids.length) return;
  if (!confirm(`Remove ${ids.length} selected run${ids.length === 1 ? "" : "s"} from dashboard history? The append-only audit log will be preserved.`)) return;
  try {
    await api("/api/runs/bulk-delete", { method: "POST", body: JSON.stringify({ ids }) });
    if (state.detail?.run && ids.includes(state.detail.run.id)) state.detail = null;
    state.selectedRuns.clear();
    await load();
  } catch (error) { alert(error.message); }
}

function exportSelectedRuns(format) {
  const ids = selectedRunIds();
  if (!ids.length) return;
  const query = encodeURIComponent(ids.join(","));
  window.open(`/api/export?format=${encodeURIComponent(format)}&run_ids=${query}`, "_blank");
}

function toggleDisclosure(id) {
  state.disclosures[id] = !state.disclosures[id];
  const panel = document.querySelector(`[data-disclosure-panel="${id}"]`);
  const trigger = panel?.querySelector("[data-disclosure]");
  if (!panel || !trigger) return;
  panel.classList.toggle("is-open", state.disclosures[id]);
  trigger.setAttribute("aria-expanded", String(state.disclosures[id]));
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("gscrape-theme", theme);
  const toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    const light = theme === "light";
    toggle.textContent = light ? "☾" : "☼";
    toggle.setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
    toggle.title = light ? "Switch to dark theme" : "Switch to light theme";
  }
}

function render() {
  const titles = { overview: "Overview", search: "New search", "raw-export": "Raw Export", history: "Run history", console: "Console", settings: "Settings", debug: "Debug" };
  const fullPageRun = location.pathname.match(/^\/run\/([A-Za-z0-9_-]+)$/)?.[1];
  if (fullPageRun) {
    $("#title").textContent = `Run ${fullPageRun}`;
    if (!state.detail?.run || state.detail.run.id !== fullPageRun) {
      shell('<div class="card empty"><p class="muted">Loading run details…</p></div>');
      loadFullPageRun(fullPageRun);
      return;
    }
    shell(`<div class="section-head page-heading full-page-heading"><div><p class="eyebrow section-kicker">FULL-PAGE RUN VIEW</p><h2>Run ${esc(fullPageRun)}</h2><p class="muted">Review email evidence with image-first rows and metadata below each message.</p></div><a class="button ghost" href="/">Back to dashboard</a></div><section class="full-page-run">${detailExpansion(state.detail.run)}</section>`);
    return;
  }
  $("#title").textContent = titles[state.view];
  document.querySelectorAll(".nav").forEach((nav) => nav.classList.toggle("active", nav.dataset.view === state.view));

  if (state.view === "overview") {
    shell(`<div class="hero card"><div class="hero-copy"><div class="hero-kicker"><span class="live-dot"></span> LOCAL RESULTS WORKSPACE</div><h2>Keep every discovery run organized.</h2><p class="muted">Each search receives a sequential six-digit ID and its own folder under <code>results/</code>.</p></div><button class="button" data-go-view="search">Start a search <span aria-hidden="true">→</span></button></div>
    <div class="grid stat-grid">
      ${stat("Search runs", state.runs.length, "tracked locally")}
      ${stat("Messages scanned", state.runs.reduce((total, run) => total + (run.scanned || 0), 0), "across recorded runs")}
      ${stat("Logged matches", state.analysis.total_matches, "from the redacted JSONL log")}
      ${stat("High-score matches", state.analysis.high_score_count, "heuristic review priority")}
    </div>
    ${disclosure("matchAnalysis", "Match analysis", "Computed locally by gscrape.py.", `<div class="analysis-overview"><div class="analysis-total"><span class="muted">Redacted findings</span><strong>${state.analysis.total_matches}</strong><small>total across all recorded runs</small></div><div class="tags">${categoryRows()}</div></div>`, state.disclosures.matchAnalysis, `<div class="actions disclosure-actions"><a class="button ghost compact" href="/api/export?format=report">Markdown</a><a class="button ghost compact" href="/api/export?format=matches-csv">CSV</a><a class="button ghost compact" href="/api/export?format=matches-json">JSON</a></div>`)}
    ${disclosure("recentActivity", "Recent activity", "Select runs to manage or merge reports in bulk.", `<div class="activity-table">${runRows({ idPrefix: "overview-runs" })}${bulkActionBar()}</div>`, state.disclosures.recentActivity, `<button class="button compact" data-go-view="search">New search <span aria-hidden="true">→</span></button>`)}`);
  }

  if (state.view === "history") {
    shell(`<div class="section-head page-heading"><div><p class="eyebrow section-kicker">ARCHIVE</p><h2>Run history</h2><p class="muted">Review, export, or remove individual discovery runs.</p></div><div class="actions">
      <a class="button ghost" href="/api/export?format=markdown">Run report</a>
      <a class="button ghost" href="/api/export?format=csv">Run CSV</a>
    </div></div><div class="section">${runRows({ idPrefix: "history-runs" })}${bulkActionBar()}</div>`);
  }

  if (state.view === "raw-export") {
    const accounts = state.coreAccounts.filter((account) => account.provider_id === "gmail");
    shell(`<div class="section-head page-heading"><div><p class="eyebrow section-kicker">UNREDACTED LOCAL ARCHIVE</p><h2>Raw mailbox export</h2><p class="muted">Exports complete Gmail API records, raw headers/body structure, and optional attachment bytes into the separate raw-mailbox database. Nothing from this workflow is sent to the normal redacted dashboard log.</p></div></div>
      <div class="card section"><form id="raw-export-form" class="form">
        <div class="field"><label for="raw-account-id">Gmail account</label><select id="raw-account-id" name="account_id" required>${accounts.map((account) => `<option value="${esc(account.id)}">${esc(account.email)} · ${esc(account.status)}</option>`).join("")}</select></div>
        <div class="field"><label for="raw-max-messages">Maximum messages</label><input id="raw-max-messages" name="max_messages" type="number" min="1" max="100000" value="10000"></div>
        <div class="field full"><label for="raw-query">Gmail query (optional)</label><input id="raw-query" name="query" placeholder="e.g. from:someone@example.com has:attachment"></div>
        <div class="field"><label for="raw-after">After (optional)</label><input id="raw-after" name="after" placeholder="YYYY/MM/DD"></div>
        <div class="field"><label for="raw-before">Before (optional)</label><input id="raw-before" name="before" placeholder="YYYY/MM/DD"></div>
        <div class="field"><label for="raw-rps">Requests per second</label><input id="raw-rps" name="requests_per_second" type="number" min="0" max="10" step="0.1" value="1"></div>
        <div class="field"><label for="raw-cooldown">Adaptive cooldown seconds</label><input id="raw-cooldown" name="rate_limit_max_delay" type="number" min="1" max="300" value="60"></div>
        <div class="field full"><div class="checkbox-setting"><label class="checkbox-label" for="raw-include-attachments"><input id="raw-include-attachments" name="include_attachments" type="checkbox"><span class="checkmark" aria-hidden="true"></span></label><label class="checkbox-setting-text" for="raw-include-attachments">Download attachment bytes</label></div><p class="muted">When disabled, all attachment metadata is retained but no attachment bytes are downloaded.</p></div>
        <div class="field"><label for="raw-extensions">Attachment extensions</label><input id="raw-extensions" name="attachment_extensions" placeholder="pdf,docx,png"></div>
        <div class="field"><label for="raw-filename-regex">Attachment filename regex</label><input id="raw-filename-regex" name="attachment_filename_regex" placeholder="(?i)\\.(pdf|docx)$"></div>
        <div class="field full"><label for="raw-resume-id">Resume export ID (optional)</label><input id="raw-resume-id" name="resume_id" placeholder="Paste an existing export ID"></div>
        <div class="actions"><button class="button">Start raw export</button></div>
      </form></div>
      <div class="card section"><div class="section-head"><div><h2>Raw export jobs</h2><p class="muted">Database: <code>raw-mailbox/raw-mailbox.db</code></p></div><button class="button ghost compact" type="button" data-raw-refresh>Refresh</button></div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Export</th><th>Status</th><th>Messages saved</th><th>Attachments</th><th>Failures</th><th>Actions</th></tr></thead><tbody>${state.rawExports.length ? state.rawExports.map((item) => `<tr class="raw-export-row" data-raw-toggle="${esc(item.id)}" tabindex="0"><td><button class="button ghost compact" type="button" data-raw-expand="${esc(item.id)}" aria-expanded="${state.expandedRawExports.has(item.id)}">${state.expandedRawExports.has(item.id) ? "▾" : "▸"} Details</button> <code>${esc(item.id.slice(0, 12))}…</code></td><td><span class="badge ${statusClass(item.status)}">${esc(item.status)}</span></td><td>${item.message_count || 0}</td><td>${item.attachment_count || 0}</td><td>${item.failure_count || 0}</td><td>${item.status === "running" ? `<button class="button ghost compact danger" type="button" data-raw-action="stop" data-raw-id="${esc(item.id)}">Stop</button>` : `<button class="button ghost compact" type="button" data-raw-action="resume" data-raw-id="${esc(item.id)}">Resume</button>`} <button class="button ghost compact danger" type="button" data-raw-action="delete" data-raw-id="${esc(item.id)}">Delete</button></td></tr>${state.expandedRawExports.has(item.id) ? rawExportDetail(item) : ""}`).join("") : '<tr><td colspan="6" class="muted">No raw exports yet.</td></tr>'}</tbody></table></div></div>`);
  }

  if (state.view === "search") {
    shell(`<div class="card"><div class="section-head"><div><h2>Build a search</h2>
      <p class="muted">Search is executed by <code>gscrape.py</code>; results remain metadata-first.</p></div></div>
      <form id="search-form" class="form">
        <div class="field"><label for="search-account-id">Search target — select one account</label><p class="muted">This search runs only against the selected mailbox. It will never search all configured accounts.</p><select id="search-account-id" name="account_id" required>
          <option value="">Choose one email account</option>${state.accounts.map((account) => `<option value="${account.id}" ${account.status !== "connected" ? "disabled" : ""}>${esc(account.display_name || account.label)} · ${esc(account.email)} — ${esc(account.status)}</option>`).join("")}
        </select></div>
        <div class="field full category-picker"><div class="section-head"><strong>Discovery sections</strong><div class="actions inline-actions"><button type="button" class="button ghost" data-select-categories="all">Select all</button><button type="button" class="button ghost" data-select-categories="none">Clear</button></div></div>
          <p class="muted">Choose one or more. Nothing is selected by default, so each scan only runs the sections you intend.</p>
          <div class="category-grid">${[
            ["attachments", "Attachments"], ["phone-numbers", "Phone Numbers"], ["contacts", "Contacts"],
            ["content", "Content"], ["crypto", "Crypto"], ["identity", "Identity"],
          ].map(([value, label]) => `<label class="category-option"><input type="checkbox" name="domains" value="${value}"><span>${label}</span></label>`).join("")}</div>
        </div>
        <div class="field full expert-options"><span class="field-label">Detector regex overrides (optional)</span><p class="muted">Only the controls for selected discovery sections are shown. Expressions are local, case-insensitive additions to the selected detector.</p>
          <div class="regex-grid">${[
            ["phone", "Phone format regex"], ["wallet-file", "Wallet filename regex"],
            ["seed", "Recovery cue/word regex"], ["crypto-address", "Crypto address regex"],
            ["gourl", "GoURL variant regex"], ["identity", "Identity variant regex"],
            ["wallet", "Wallet terminology regex"],
          ].map(([value, label]) => `<label data-regex-category="${value}" hidden>${label}<input class="category-regex" data-category="${value}" disabled placeholder="Optional regex"></label>`).join("")}</div>
        </div>
        <div class="field full attachment-filter" data-category-panel="attachments" hidden><div class="section-head"><strong>Attachment filters</strong><span class="badge">Filename discovery</span></div><p class="muted">Match attachment filenames locally. Optionally narrow by type or filename. Turn on acquisition only when you want bytes saved for dashboard preview/download.</p>
          <div data-attachment-options><div class="category-grid">${Object.entries({documents:"Documents",spreadsheets:"Spreadsheets",archives:"Archives",images:"Images",wallets:"Wallet / backup files",email:"Email exports"}).map(([value,label]) => `<label class="category-option"><input type="checkbox" name="attachment_type" value="${value}" disabled><span>${label}</span></label>`).join("")}</div>
          <label for="search-attachment-extensions">Custom file types</label><input id="search-attachment-extensions" name="attachment_extensions" disabled placeholder="pdf,dat,custom">
          <label for="search-attachment-regex">Attachment filename regex</label><input id="search-attachment-regex" name="attachment_filename_regex" disabled placeholder="invoice[-_].+\\.pdf$">
          <label class="category-option acquire-option"><input type="checkbox" name="acquire_attachments" value="1" checked disabled><span>Acquire matching attachments for dashboard thumbnails and download</span></label></div>
        </div>
        <div class="field full contact-filter" data-category-panel="contacts" hidden><div class="section-head"><strong>Contact filters</strong><span class="badge">Metadata only</span></div><p class="muted">Optionally narrow contacts by email address or domain. These filters inspect message metadata, not message bodies.</p>
          <label for="contact-email-regex">Email address regex</label><input id="contact-email-regex" name="contact_email_regex" disabled placeholder=".+@example\\.com$">
          <label for="contact-email-domain">Email domain</label><input id="contact-email-domain" name="contact_email_domain" disabled placeholder="example.com">
        </div>
        <div class="field"><label for="search-max-messages">Maximum messages</label><input id="search-max-messages" name="max_messages" type="number" value="" min="1" max="10000" placeholder="No limit (up to 10000)"><p class="muted">Leave blank for no practical limit. Only set a number when you want to cap the scan.</p></div>
        <div class="field"><label for="search-rps">Maximum Gmail requests/second</label><input id="search-rps" name="requests_per_second" type="number" value="1" min="0" max="10" step="0.1"><p class="muted">Local pacing ceiling. 0 disables proactive pacing; quota errors still use bounded adaptive backoff.</p></div>
        <div class="field"><label for="search-cooldown">Maximum adaptive cooldown (seconds)</label><input id="search-cooldown" name="rate_limit_max_delay" type="number" value="60" min="1" max="300" step="1"><p class="muted">Upper bound after rate-limit responses.</p></div>
        <div class="field"><label for="search-query">Gmail query (optional)</label><input id="search-query" name="query" placeholder="e.g. from:someone@example.com has:attachment"></div>
        <div class="field full date-window"><div class="section-head"><strong>Date window (optional)</strong><button type="button" class="button ghost compact" data-historical-window>Use 2013–2018</button></div>
          <p class="muted">Leave blank for unrestricted search. Dates are applied only when you enter them.</p>
          <div class="date-fields"><label for="search-after">After</label><input id="search-after" name="after_date" placeholder="YYYY/MM/DD"><label for="search-before">Before (inclusive)</label><input id="search-before" name="before_date" placeholder="YYYY/MM/DD"></div>
        </div>
        <div class="field full"><label for="search-profile-note">Local profile note (optional)</label><textarea id="search-profile-note" name="regex" placeholder="Saved for future local refinement; not sent to Gmail"></textarea></div>
        <div class="actions"><button class="button">Start search</button><button type="button" class="button ghost" data-save-profile>Save profile</button></div>
      </form></div>
      <div class="card section"><div class="section-head"><h2>Refined query suggestions</h2><label for="suggest-category">Category</label><select id="suggest-category"><option value="all">All categories</option><option value="gourl">GoURL</option><option value="wallet">Wallet</option><option value="seed">Recovery</option><option value="wallet-file">Wallet files</option><option value="crypto-address">Addresses</option><option value="identity">Identity</option><option value="phone">Phone numbers</option></select></div><div id="suggestions" class="suggestions"><span class="muted">Choose a category to load suggestions.</span></div></div>`);
  }

  if (state.view === "console") {
    shell(`<div class="console" id="console">GScrape local console\nType: runs | report | export matches-csv | status\n\n${state.runs.map((run) => `[${run.status}] ${run.category} — ${run.matches || 0} matches`).join("\n")}</div>
      <div class="cmd"><input id="command" placeholder="Enter an approved command…"><button class="button" data-run-command>Run</button></div>`);
  }

  if (state.view === "settings") {
    shell(`<div class="card"><div class="section-head"><div><h2>Email accounts</h2><p class="muted">OAuth sessions are saved locally with restrictive permissions.</p></div></div>
      <form id="account-form" class="form compact-form">
        <div class="field"><label for="account-name">Display name</label><input id="account-name" name="name" required maxlength="100" placeholder="Personal Gmail"></div>
        <div class="field"><label for="account-email">Email address</label><input id="account-email" name="email" type="email" required maxlength="254" placeholder="you@example.com"></div>
        <div class="field"><label for="account-avatar">Thumbnail initials</label><input id="account-avatar" name="avatar" maxlength="3" placeholder="Optional"></div>
        <div class="field"><span class="field-label">Identity preview</span><div class="account-preview"><span class="avatar" id="avatar-preview">?</span><span id="name-preview">New account</span></div></div>
        <div class="actions full"><button class="button">Add email account</button><button type="reset" class="button ghost">Clear</button></div>
      </form>
      ${state.accounts.length ? `<div class="table-wrap section"><table class="table"><thead><tr><th>Account</th><th>Email</th><th>Status</th><th>Actions</th></tr></thead><tbody>${state.accounts.map((account) => `<tr>
        <td><span class="avatar small">${esc(account.avatar)}</span> ${esc(account.display_name || account.label)}</td><td>${esc(account.email)}</td><td><span class="badge ${account.status === "connected" ? "green" : ""}">${esc(account.status)}</span></td>
        <td><span data-account-status="${account.id}" class="badge ${account.status === "connected" ? "green" : ""}">${esc(account.status)}</span>
        <button class="button ghost" data-account-action="authorize" data-account-id="${esc(account.id)}">Authorize</button>
        <button class="button ghost" data-account-action="profile" data-account-id="${esc(account.id)}">Profile</button>
        <button class="button ghost" data-account-action="labels" data-account-id="${esc(account.id)}">Labels</button>
        <button class="button ghost" data-account-action="remove" data-account-id="${esc(account.id)}">Remove</button></td>
      </tr>`).join("")}</tbody></table></div>` : '<div class="empty">No email accounts configured.</div>'}</div>
      <div class="card section"><div class="section-head"><div><h2>Logging & troubleshooting</h2><p class="muted">Dated local logs with size-based rolling. Secrets and OAuth values are not logged.</p></div><button class="button ghost" data-refresh-logging>Refresh</button></div>
        <form id="logging-form" class="form compact-form">
          <div class="field"><label for="log-level">Log level</label><select id="log-level" name="level"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select></div>
          <div class="field"><label for="log-max-mb">Max file size (MB)</label><input id="log-max-mb" name="max_mb" type="number" min="1" max="50" value="${Math.round(state.logging.config.max_bytes / 1048576)}"></div>
          <div class="field"><label for="log-backup-count">Retained files</label><input id="log-backup-count" name="backup_count" type="number" min="1" max="20" value="${state.logging.config.backup_count}"></div>
          <div class="actions full"><button class="button">Save logging settings</button></div>
        </form>
        <p class="muted">Log directory: <code>logs/</code> · Files: ${state.logging.files.length}</p>
        <pre class="log-tail">${esc((state.logging.tail || []).join("\n") || "No dashboard log entries yet.")}</pre>
      </div>
      <div class="card section"><h2>Shared account control plane</h2><p class="muted">These provider-neutral records drive new dashboard jobs. OAuth state is tracked locally; tokens and credentials are never returned by this API.</p>
      ${state.coreAccounts.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Provider</th><th>Email</th><th>Status</th><th>Legacy session</th><th>Actions</th></tr></thead><tbody>${state.coreAccounts.map((account) => `<tr><td>${esc(account.provider_id)}</td><td>${esc(account.email)}</td><td><span class="badge">${esc(account.status)}</span></td><td>${account.legacy_session?.has_refresh_token ? "Available" : "Not connected"}</td><td><button class="button ghost compact" type="button" data-core-connect="${esc(account.id)}">Connect</button></td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">No shared control-plane accounts yet. Add an account above to register it.</div>'}</div>
      <div class="card section"><h2>Workspace</h2><p class="muted">Source of truth: <code>gscrape.py</code>. Audit log: <code>email-wallet-search.log</code> · Database: <code>dashboard.db</code></p></div>`);
    const loggingForm = $("#logging-form");
    if (loggingForm) loggingForm.elements.level.value = state.logging.config.level;
    const accountForm = $("#account-form");
    if (accountForm) {
      accountForm.elements.name.oninput = () => {
        $("#name-preview").textContent = accountForm.elements.name.value || "New account";
        $("#avatar-preview").textContent = (accountForm.elements.avatar.value || accountForm.elements.name.value || "?").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
      };
      accountForm.elements.avatar.oninput = accountForm.elements.name.oninput;
    }
  }

  if (state.view === "debug") {
    const snapshot = state.debug;
    shell(`<div class="section-head"><div><h2>System diagnostics</h2><p class="muted">Local-only checks for the dashboard, scanner, storage, and logging.</p></div><div class="actions"><button class="button" data-debug-refresh>Refresh checks</button><button class="button ghost" data-debug-clear>Clear console</button></div></div>
      ${snapshot ? `<div class="card"><div class="debug-summary"><span class="badge ${snapshot.overall ? "green" : ""}">${snapshot.overall ? "Operational" : "Attention needed"}</span><span class="muted">${esc(new Date(snapshot.timestamp).toLocaleString())}</span></div><div class="debug-grid">${snapshot.checks.map((check) => `<div class="debug-check"><span class="badge ${check.ok ? "green" : ""}">${check.ok ? "PASS" : "FAIL"}</span><div><strong>${esc(check.name)}</strong><div class="muted">${esc(check.detail)}</div></div></div>`).join("")}</div></div><div class="card section"><h2>Recent dashboard log</h2><pre class="log-tail">${esc((snapshot.logging.tail || []).join("\n") || "No log entries.")}</pre></div>` : '<div class="card empty">Loading diagnostics…</div>'}
      <div class="card section"><h2>Live browser console</h2><p class="muted">Captures JavaScript errors, rejected promises, CSP violations, and dashboard API requests without using eval.</p><pre class="log-tail" data-live-debug>${esc(state.debugConsole.map((entry) => `[${entry.time}] ${entry.type.toUpperCase()} ${entry.message}${entry.detail ? ` — ${entry.detail}` : ""}`).join("\n") || "No browser errors or network events recorded.")}</pre></div>`);
  }
}

async function loadSuggestions() {
  const category = $("#suggest-category").value;
  try {
    const payload = await api(`/api/suggestions?category=${encodeURIComponent(category)}`);
    $("#suggestions").innerHTML = Object.entries(payload.suggestions).flatMap(([name, items]) =>
      items.map((item) => `<button class="suggestion" data-suggestion="${esc(item)}"><strong>${esc(name)}</strong> ${esc(item)}</button>`),
    ).join("") || '<span class="muted">No suggestions available.</span>';
  } catch (error) { $("#suggestions").textContent = error.message; }
}

function useSuggestion(query) {
  const input = document.querySelector('input[name="query"]');
  if (input) input.value = query;
}

function setCategories(selected) {
  document.querySelectorAll('input[name="domains"]').forEach((input) => {
    input.checked = selected;
    toggleCategoryPanel(input.value, selected);
  });
}

function toggleCategoryPanel(category, selected) {
  const panel = document.querySelector(`[data-category-panel="${category}"]`);
  if (panel) {
    panel.hidden = !selected;
    panel.querySelectorAll("input").forEach((input) => { input.disabled = !selected; });
  }
  document.querySelectorAll(`[data-regex-category]`).forEach((label) => {
    const active = selected && label.dataset.regexCategory === category;
    if (active || label.dataset.regexCategory === category) {
      label.hidden = !active;
      const input = label.querySelector("input");
      if (input) input.disabled = !active;
    }
  });
}

function collectSearchConfig(form) {
  const data = Object.fromEntries(new FormData(form));
  data.domains = [...form.querySelectorAll('input[name="domains"]:checked')].map((input) => input.value);
  const attachmentDiscovery = data.domains.includes("attachments");
  if (!data.domains.length) throw Error("Select at least one discovery section.");
  data.attachment_discovery = attachmentDiscovery;
  data.categories = data.domains.includes("crypto") ? ["wallet", "seed", "wallet-file", "crypto-address", "gourl"] : [];
  data.category = data.categories.length === 7 ? "all" : (data.categories[0] || "all");
  data.category_regex = {};
  form.querySelectorAll(".category-regex").forEach((input) => {
    if (input.value.trim()) data.category_regex[input.dataset.category] = input.value.trim();
  });
  if (data.contact_email_regex) data.category_regex.identity = data.contact_email_regex;
  const typeExtensions = new Set();
  form.querySelectorAll('input[name="attachment_type"]:checked').forEach((input) => {
    const groups = { documents: ["pdf","doc","docx","odt","rtf","txt","csv"], spreadsheets: ["xls","xlsx","ods","csv"], archives: ["zip","7z","rar","tar","gz","bz2"], images: ["jpg","jpeg","png","gif","tif","tiff","webp"], wallets: ["dat","wallet","key","json","backup","bak","db","sqlite"], email: ["eml","msg"] };
    (groups[input.value] || []).forEach((extension) => typeExtensions.add(extension));
  });
  const custom = String(data.attachment_extensions || "").split(",").map((value) => value.trim()).filter(Boolean);
  data.attachment_extensions = [...typeExtensions, ...custom];
  data.acquire_attachments = Boolean(form.querySelector('input[name="acquire_attachments"]')?.checked);
  if (!data.attachment_discovery) {
    data.attachment_extensions = [];
    data.attachment_filename_regex = "";
    data.acquire_attachments = false;
  }
  const rawMax = String(data.max_messages ?? "").trim();
  if (rawMax === "") delete data.max_messages;
  else data.max_messages = Number(rawMax);
  data.after_date = String(data.after_date || "").trim();
  data.before_date = String(data.before_date || "").trim();
  const rawRps = String(data.requests_per_second ?? "").trim();
  data.requests_per_second = rawRps === "" ? 1 : Number(rawRps);
  const rawCooldown = String(data.rate_limit_max_delay ?? "").trim();
  data.rate_limit_max_delay = rawCooldown === "" ? 60 : Number(rawCooldown);
  return data;
}

function applyHistoricalWindow() {
  const form = $("#search-form");
  if (!form) return;
  form.elements.after_date.value = "2013/01/01";
  form.elements.before_date.value = "2018/12/31";
}

async function addAccount(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try {
    await api("/api/accounts", { method: "POST", body: JSON.stringify(data) });
    await api("/api/core/accounts", { method: "POST", body: JSON.stringify({ provider_id: "gmail", email: data.email, label: data.name }) });
    await refreshAccounts();
    await refreshCoreAccounts();
    event.target.reset();
    render();
  }
  catch (error) { alert(error.message); }
}

async function saveLogging(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.max_bytes = Number(data.max_mb) * 1048576;
  data.backup_count = Number(data.backup_count);
  delete data.max_mb;
  try { await api("/api/logging", { method: "POST", body: JSON.stringify(data) }); await load(); go("settings"); }
  catch (error) { alert(error.message); }
}

async function refreshLogging() {
  try { state.logging = await api("/api/logging"); render(); }
  catch (error) { alert(error.message); }
}

async function connectCoreAccount(id) {
  try {
    const payload = await api(`/api/core/accounts/${encodeURIComponent(id)}/connect`, { method: "POST", body: "{}" });
    window.open(payload.authorization_url, "_blank", "noopener,noreferrer");
    let attempts = 0;
    const timer = setInterval(async () => {
      attempts += 1;
      try { await refreshCoreAccounts(true); } catch (_) {}
      const account = state.coreAccounts.find((item) => item.id === id);
      if (account?.status === "connected") {
        clearInterval(timer);
        alert(`Connected: ${account.email}. Control is back — you can start a scan.`);
        go("settings");
        return;
      }
      if (attempts >= 90) {
        clearInterval(timer);
        alert("Still waiting for Google authorization. Finish consent in the popup, or click Connect again.");
      }
    }, 1000);
  } catch (error) { alert(error.message); }
}

function consumeOAuthReturn() {
  const params = new URLSearchParams(window.location.search);
  const oauth = params.get("oauth");
  if (!oauth) return;
  const accountId = params.get("account_id") || "";
  const oauthError = params.get("oauth_error") || "";
  const oauthMessage = params.get("oauth_message") || "";
  history.replaceState({}, "", "/");
  go("settings");
  refreshCoreAccounts(true).then(() => {
    if (oauth === "connected") {
      const account = state.coreAccounts.find((item) => item.id === accountId) || state.coreAccounts.find((item) => item.status === "connected");
      alert(account ? `Connected: ${account.email}. Control is back.` : "Account connected. Control is back.");
    } else {
      const detail = oauthMessage || (oauthError ? `OAuth error: ${oauthError}` : "");
      alert(detail || "Google authorization did not complete. Click Connect and try again.");
    }
  }).catch(() => {});
}

async function authorize(id) {
  try {
    await api(`/api/accounts/${id}/authorize`, { method: "POST", body: "{}" });
    alert("OAuth authorization opened. Complete it in the browser. Account status will update automatically.");
    let attempts = 0;
    const timer = setInterval(async () => {
      attempts += 1;
      try { await refreshAccounts(); } catch (_) {}
      if (state.accounts.find((account) => account.id === id)?.status === "connected" || attempts >= 60) clearInterval(timer);
    }, 1000);
  }
  catch (error) { alert(error.message); }
}

async function profileCheck(id) {
  try { const result = await api(`/api/accounts/${id}/profile`, { method: "POST", body: "{}" }); alert(JSON.stringify(result.profile, null, 2)); }
  catch (error) { alert(error.message); }
}

async function showLabels(id) {
  try {
    const result = await api(`/api/accounts/${id}/labels`, { method: "POST", body: "{}" });
    alert((result.labels || []).map((label) => `${label.id}  ${label.name}`).join("\n") || "No labels returned.");
  } catch (error) { alert(error.message); }
}

async function removeAccount(id) {
  if (!confirm("Remove this saved session?")) return;
  await api(`/api/accounts/${id}/remove`, { method: "POST", body: "{}" });
  await load(); go("settings");
}

function go(view) {
  state.view = view;
  render();
  if (view === "settings") refreshCoreAccounts(true).catch(() => {});
  if (view === "search") loadSuggestions();
  if (view === "debug") refreshDebug();
  if (view === "raw-export") refreshRawExports();
}

async function refreshRawExports() {
  try {
    state.rawExports = (await api("/api/raw-exports")).exports || [];
    if (state.view !== "raw-export") return;
    const form = $("#raw-export-form");
    const formState = form ? {
      account_id: form.elements.account_id?.value || "",
      max_messages: form.elements.max_messages?.value || "",
      query: form.elements.query?.value || "",
      after: form.elements.after?.value || "",
      before: form.elements.before?.value || "",
      requests_per_second: form.elements.requests_per_second?.value || "",
      rate_limit_max_delay: form.elements.rate_limit_max_delay?.value || "",
      include_attachments: Boolean(form.elements.include_attachments?.checked),
      attachment_extensions: form.elements.attachment_extensions?.value || "",
      attachment_filename_regex: form.elements.attachment_filename_regex?.value || "",
      resume_id: form.elements.resume_id?.value || "",
    } : null;
    render();
    if (!formState) return;
    const updated = $("#raw-export-form");
    Object.entries(formState).forEach(([name, value]) => {
      const control = updated.elements[name];
      if (!control) return;
      if (control.type === "checkbox") control.checked = value;
      else control.value = value;
    });
  } catch (_) {}
}

async function submitRawExport(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.include_attachments = event.target.elements.include_attachments.checked;
  data.max_messages = Number(data.max_messages);
  data.attachment_extensions = String(data.attachment_extensions || "").split(",").map((value) => value.trim()).filter(Boolean);
  data.requests_per_second = Number(data.requests_per_second);
  data.rate_limit_max_delay = Number(data.rate_limit_max_delay);
  try { await api("/api/raw-exports", { method: "POST", body: JSON.stringify(data) }); await refreshRawExports(); } catch (error) { alert(error.message); }
}

async function stopRawExport(id) {
  if (!confirm("Stop this raw export? Completed messages remain resumable.")) return;
  try { await api(`/api/raw-exports/${encodeURIComponent(id)}/stop`, { method: "POST", body: "{}" }); await refreshRawExports(); } catch (error) { alert(error.message); }
}

async function resumeRawExport(id) {
  const form = $("#raw-export-form");
  if (!form) return;
  form.elements.resume_id.value = id;
  form.scrollIntoView({ behavior: "smooth" });
}

async function deleteRawExport(id) {
  if (!confirm("Delete this raw export index? Raw blob files are not automatically deleted.")) return;
  try { await api(`/api/raw-exports/${encodeURIComponent(id)}/delete`, { method: "POST", body: "{}" }); await refreshRawExports(); } catch (error) { alert(error.message); }
}

async function submit(event) {
  event.preventDefault();
  try {
    const data = collectSearchConfig(event.target);
    await api("/api/runs", { method: "POST", body: JSON.stringify(data) });
    go("history"); setTimeout(load, 500);
  }
  catch (error) { alert(error.message); }
}

async function saveProfile() {
  let data;
  try { data = collectSearchConfig($("#search-form")); }
  catch (error) { alert(error.message); return; }
  data.name = prompt("Profile name", "My search") || "Untitled search";
  try { await api("/api/profiles", { method: "POST", body: JSON.stringify(data) }); alert("Profile saved locally."); }
  catch (error) { alert(error.message); }
}

function command() {
  const input = $("#command");
  const output = $("#console");
  const commandText = input.value.trim();
  if (commandText === "runs") output.textContent += `\n> ${commandText}\n${state.runs.map((run) => `${run.status} ${run.category} ${run.matches || 0} matches`).join("\n")}`;
  else if (commandText === "report") window.open("/api/export?format=report", "_blank");
  else if (commandText.startsWith("export ")) window.open(`/api/export?format=${encodeURIComponent(commandText.split(/\s+/)[1] || "markdown")}`, "_blank");
  else if (commandText === "status") output.textContent += "\n> status\nGScrape local service healthy.";
  else output.textContent += `\n> ${commandText}\nUnknown approved command. Try: runs | report | export matches-csv | status`;
  input.value = "";
}

document.addEventListener("click", (event) => {
  const nav = event.target.closest(".nav[data-view]");
  if (nav) { go(nav.dataset.view); return; }
  const viewButton = event.target.closest("[data-go-view]");
  if (viewButton) { go(viewButton.dataset.goView); return; }
  const rawRefresh = event.target.closest("[data-raw-refresh]");
  if (rawRefresh) { refreshRawExports(); return; }
  const rawAction = event.target.closest("[data-raw-action]");
  if (rawAction) {
    const id = rawAction.dataset.rawId;
    if (rawAction.dataset.rawAction === "stop") stopRawExport(id);
    if (rawAction.dataset.rawAction === "resume") resumeRawExport(id);
    if (rawAction.dataset.rawAction === "delete") deleteRawExport(id);
    return;
  }
  const rawMessages = event.target.closest("[data-raw-messages]");
  if (rawMessages) { loadRawMessages(rawMessages.dataset.rawMessages); return; }
  const rawFilterClear = event.target.closest("[data-raw-filter-clear]");
  if (rawFilterClear) {
    const exportId = rawFilterClear.dataset.rawFilterClear;
    state.rawMessages[exportId] = { ...(state.rawMessages[exportId] || {}), filters: {}, offset: 0 };
    loadRawMessages(exportId, 0);
    return;
  }
  const rawMessageDetailButton = event.target.closest("[data-raw-message-detail]");
  if (rawMessageDetailButton) {
    loadRawMessageDetail(rawMessageDetailButton.dataset.rawMessageDetail, rawMessageDetailButton.dataset.rawMessageId);
    return;
  }
  const rawMessagesNext = event.target.closest("[data-raw-messages-next]");
  if (rawMessagesNext) {
    const exportId = rawMessagesNext.dataset.rawMessagesNext;
    const payload = state.rawMessages[exportId];
    loadRawMessages(exportId, (payload?.offset || 0) + (payload?.limit || 25));
    return;
  }
  const rawMessagesPrev = event.target.closest("[data-raw-messages-prev]");
  if (rawMessagesPrev) {
    const exportId = rawMessagesPrev.dataset.rawMessagesPrev;
    const payload = state.rawMessages[exportId];
    loadRawMessages(exportId, Math.max(0, (payload?.offset || 0) - (payload?.limit || 25)));
    return;
  }
  const rawExpand = event.target.closest("[data-raw-expand]");
  if (rawExpand) {
    const id = rawExpand.dataset.rawExpand;
    if (state.expandedRawExports.has(id)) state.expandedRawExports.delete(id);
    else state.expandedRawExports.add(id);
    render();
    return;
  }
  const saveProfileButton = event.target.closest("[data-save-profile]");
  if (saveProfileButton) { saveProfile(); return; }
  const runCommand = event.target.closest("[data-run-command]");
  if (runCommand) { command(); return; }
  const accountAction = event.target.closest("[data-account-action]");
  if (accountAction) {
    const action = accountAction.dataset.accountAction;
    const id = accountAction.dataset.accountId;
    if (action === "authorize") authorize(id);
    if (action === "profile") profileCheck(id);
    if (action === "labels") showLabels(id);
    if (action === "remove") removeAccount(id);
    return;
  }
  const coreConnect = event.target.closest("[data-core-connect]");
  if (coreConnect) { connectCoreAccount(coreConnect.dataset.coreConnect); return; }
  const downloadSelected = event.target.closest("[data-download-selected]");
  if (downloadSelected) { downloadSelectedAttachments(); return; }
  const debugRefresh = event.target.closest("[data-debug-refresh]");
  if (debugRefresh) { refreshDebug(); return; }
  const debugClear = event.target.closest("[data-debug-clear]");
  if (debugClear) { state.debugConsole = []; render(); return; }
  const categoryAction = event.target.closest("[data-select-categories]");
  if (categoryAction) { setCategories(categoryAction.dataset.selectCategories === "all"); return; }
  const historical = event.target.closest("[data-historical-window]");
  if (historical) { applyHistoricalWindow(); return; }
  const suggestion = event.target.closest("[data-suggestion]");
  if (suggestion) { useSuggestion(suggestion.dataset.suggestion); return; }
  const themeToggle = event.target.closest("[data-theme-toggle]");
  if (themeToggle) {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    return;
  }
  const disclosureTrigger = event.target.closest("[data-disclosure]");
  if (disclosureTrigger) {
    toggleDisclosure(disclosureTrigger.dataset.disclosure);
    return;
  }
  const runAction = event.target.closest("[data-run-action]");
  if (runAction) {
    const id = runAction.dataset.runId;
    if (runAction.dataset.runAction === "details") showRun(id);
    if (runAction.dataset.runAction === "delete") deleteRun(id);
    if (["stop", "resume", "retry"].includes(runAction.dataset.runAction)) changeRunState(id, runAction.dataset.runAction);
    return;
  }
  const runToggle = event.target.closest("[data-run-toggle]");
  if (runToggle && !event.target.closest("a, button, input, label, .run-actions, .run-select")) {
    showRun(runToggle.dataset.runToggle);
    return;
  }
  const bulkAction = event.target.closest("[data-bulk-action]");
  if (bulkAction) {
    const action = bulkAction.dataset.bulkAction;
    if (action === "delete") deleteSelectedRuns();
    else exportSelectedRuns(action);
  }
});

document.addEventListener("submit", (event) => {
  const filters = event.target.closest("[data-raw-message-filters]");
  if (filters) applyRawMessageFilters(event);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const runToggle = event.target.closest("[data-run-toggle]");
  if (!runToggle || event.target.closest("a, button, input, label")) return;
  event.preventDefault();
  showRun(runToggle.dataset.runToggle);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const rawRow = event.target.closest("[data-raw-toggle]");
  if (!rawRow || event.target.closest("button, a, input, label")) return;
  event.preventDefault();
  const id = rawRow.dataset.rawToggle;
  if (state.expandedRawExports.has(id)) state.expandedRawExports.delete(id);
  else state.expandedRawExports.add(id);
  render();
});
document.addEventListener("change", (event) => {
  if (event.target.matches('input[name="domains"]')) toggleCategoryPanel(event.target.value, event.target.checked);
  if (event.target.matches("#suggest-category")) loadSuggestions();
  if (event.target.matches("[data-select-all-attachments]")) { toggleAllAttachments(event.target.checked); return; }
  if (event.target.matches(".run-checkbox")) {
    if (event.target.checked) state.selectedRuns.add(event.target.dataset.runId);
    else state.selectedRuns.delete(event.target.dataset.runId);
    render();
    return;
  }
  if (event.target.matches(".select-all-runs")) {
    if (event.target.checked) state.runs.forEach((run) => state.selectedRuns.add(run.id));
    else state.runs.forEach((run) => state.selectedRuns.delete(run.id));
    render();
  }
});
applyTheme(localStorage.getItem("gscrape-theme") || "dark");
document.addEventListener("submit", (event) => {
  if (event.target.id === "search-form") submit(event);
  if (event.target.id === "account-form") addAccount(event);
  if (event.target.id === "logging-form") saveLogging(event);
  if (event.target.id === "raw-export-form") submitRawExport(event);
});
document.addEventListener("input", (event) => {
  if (event.target.matches("#account-name, #account-avatar")) {
    const form = $("#account-form");
    if (form?.elements.name) {
      $("#name-preview").textContent = form.elements.name.value || "New account";
      $("#avatar-preview").textContent = (form.elements.avatar.value || form.elements.name.value || "?").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
    }
  }
});
load().then(() => refreshCoreAccounts(true)).then(() => {
  consumeOAuthReturn();
  setInterval(() => {
    refreshAccounts().catch(() => {});
    refreshCoreAccounts().catch(() => {});
    if (state.view === "overview" || state.view === "history" || state.detail) {
      refreshRunState().catch(() => {});
    }
    if (state.view === "raw-export") refreshRawExports().catch(() => {});
  }, 1500);
})
  .catch((error) => shell(`<div class="card"><h2>Dashboard unavailable</h2><p class="muted">${esc(error.message)}</p></div>`));
