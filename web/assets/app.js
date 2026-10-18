const state = {
  view: "overview",
  runs: [],
  profiles: [],
  accounts: [],
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
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw Error(payload.error || "Request failed");
  return payload;
}

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

function coreJobRows(jobs) {
  return (jobs || []).map((job) => `<tr><td><code>${esc(job.id)}</code></td><td>${esc(job.provider)}</td><td><span class="badge ${statusClass(job.status)}">${esc(job.status)}</span></td><td>${esc(job.created_at || "")}</td><td>${esc(job.error_code || "")}</td></tr>`).join("");
}

async function refreshCoreJobs() {
  try {
    const payload = await api("/api/core/jobs");
    const node = document.querySelector("[data-core-jobs]");
    if (node) node.innerHTML = coreJobRows(payload.jobs);
  } catch (_) { /* legacy dashboard remains usable if the control plane is unavailable */ }
}

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

function detailExpansion(run) {
  if (!state.detail?.run || state.detail.run.id !== run.id) return "";
  const detail = state.detail;
  const summary = detail.summary || {};
  const findings = detail.matches || [];
  const error = detail.run.error ? `<div class="run-error"><strong>Run issue</strong><span>${esc(detail.run.error)}</span></div>` : "";
  return `<tr class="detail-row"><td colspan="9"><div class="run-expansion">
    <div class="run-expansion-head"><div><span class="eyebrow section-kicker">RUN DETAIL · ${esc(run.id)}</span><h3>${esc(detail.note || "Redacted run details")}</h3></div><button class="button ghost compact" type="button" data-run-action="details" data-run-id="${esc(run.id)}">Close</button></div>
    <div class="run-metrics">
      <div><span>Pages parsed</span><strong>${run.pages || 0}</strong></div>
      <div><span>Messages returned</span><strong>${run.listed || 0}</strong></div>
      <div><span>Messages fetched</span><strong>${run.scanned || 0}</strong></div>
      <div><span>Matches found</span><strong>${summary.total_matches ?? run.matches ?? 0}</strong></div>
    </div>
    ${error}
    <div class="run-expansion-section"><div class="section-head"><div><strong>Findings</strong><span class="muted">Redacted metadata only</span></div><a class="button ghost compact" href="/api/export?format=run-report&run_id=${encodeURIComponent(run.id)}">Export</a></div>
      ${findings.length ? `<div class="table-wrap mini-table"><table class="table"><thead><tr><th>Date</th><th>Subject</th><th>Categories</th><th>Location</th></tr></thead><tbody>${findings.map((match) => `<tr><td>${esc(match.message_date)}</td><td>${esc(match.subject)}</td><td>${esc((match.categories || []).join(", "))}</td><td><a class="button ghost compact" target="_blank" href="${esc(match.message_location)}">Open Gmail</a></td></tr>`).join("")}</tbody></table></div>` : '<div class="empty compact-empty">No categorized findings recorded yet.</div>'}
    </div>
    <p class="muted run-files">Result folder: <code>${esc(detail.files?.results_dir || "")}</code><br>Scan log: <code>${esc(detail.files?.scan_log || "")}</code></p>
  </div></td></tr>`;
}

function runRows({ idPrefix = "runs" } = {}) {
  if (!state.runs.length) return '<div class="card empty">No runs yet. Create your first search to begin.</div>';
  const selected = selectedRunIds();
  const allSelected = selected.length === state.runs.length;
  return `<div class="table-toolbar"><label class="select-all-control"><span class="checkbox-label"><input type="checkbox" class="select-all-runs" ${allSelected ? "checked" : ""} aria-label="Select all runs"><span class="checkmark"></span></span><span>Select all</span></label><span class="muted">${state.runs.length} recorded run${state.runs.length === 1 ? "" : "s"}</span></div><div class="table-wrap"><table class="table"><thead><tr>
    <th class="select-column"></th>
    <th>Run ID</th><th>Status</th><th>Category</th><th>Query</th><th>Scanned</th><th>Matches</th><th>Started</th><th>Actions</th>
  </tr></thead><tbody>${state.runs.map((run) => `<tr>
    <td class="select-column"><label class="checkbox-label"><input type="checkbox" class="run-checkbox" data-run-id="${esc(run.id)}" ${selected.includes(run.id) ? "checked" : ""} aria-label="Select run ${esc(run.id)}"><span class="checkmark"></span></label></td>
    <td><code class="run-id">${esc(run.id)}</code></td><td><span class="badge ${statusClass(run.status)}"><span class="status-dot"></span>${esc(run.status)}</span></td>
    <td>${esc(run.category)}</td><td>${esc(run.query || "preset")}</td>
    <td>${run.scanned || 0}</td><td>${run.matches || 0}</td>
    <td>${esc(new Date(run.started_at).toLocaleString())}</td>
    <td><div class="row-actions"><button class="button ghost compact" type="button" data-run-action="details" data-run-id="${esc(run.id)}" aria-expanded="${state.detail?.run?.id === run.id}">${state.detail?.run?.id === run.id ? "Close" : "Details"}</button><button class="button ghost compact danger" type="button" data-run-action="delete" data-run-id="${esc(run.id)}">Remove</button></div></td>
  </tr>${detailExpansion(run)}`).join("")}</tbody></table></div>`;
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
  const [runs, profiles, accounts, analysis, logging] = await Promise.all([
    api("/api/runs"), api("/api/profiles"), api("/api/accounts"), api("/api/analysis"), api("/api/logging"),
  ]);
  state.runs = runs.runs;
  state.profiles = profiles.profiles;
  state.accounts = accounts.accounts;
  state.analysis = analysis.summary;
  state.selectedRuns = new Set([...state.selectedRuns].filter((id) => state.runs.some((run) => run.id === id)));
  state.logging = logging;
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
  const titles = { overview: "Overview", search: "New search", history: "Run history", console: "Console", settings: "Settings", debug: "Debug" };
  $("#title").textContent = titles[state.view];
  document.querySelectorAll(".nav").forEach((nav) => nav.classList.toggle("active", nav.dataset.view === state.view));

  if (state.view === "overview") {
    shell(`<div class="hero card"><div class="hero-copy"><div class="hero-kicker"><span class="live-dot"></span> LOCAL RESULTS WORKSPACE</div><h2>Keep every discovery run organized.</h2><p class="muted">Each search receives a sequential six-digit ID and its own folder under <code>results/</code>.</p></div><button class="button" onclick="go('search')">Start a search <span aria-hidden="true">→</span></button></div>
    <div class="grid stat-grid">
      ${stat("Search runs", state.runs.length, "tracked locally")}
      ${stat("Messages scanned", state.runs.reduce((total, run) => total + (run.scanned || 0), 0), "across recorded runs")}
      ${stat("Logged matches", state.analysis.total_matches, "from the redacted JSONL log")}
      ${stat("High-score matches", state.analysis.high_score_count, "heuristic review priority")}
    </div>
    ${disclosure("matchAnalysis", "Match analysis", "Computed locally by gscrape.py.", `<div class="analysis-overview"><div class="analysis-total"><span class="muted">Redacted findings</span><strong>${state.analysis.total_matches}</strong><small>total across all recorded runs</small></div><div class="tags">${categoryRows()}</div></div>`, state.disclosures.matchAnalysis, `<div class="actions disclosure-actions"><a class="button ghost compact" href="/api/export?format=report">Markdown</a><a class="button ghost compact" href="/api/export?format=matches-csv">CSV</a><a class="button ghost compact" href="/api/export?format=matches-json">JSON</a></div>`)}
    ${disclosure("recentActivity", "Recent activity", "Select runs to manage or merge reports in bulk.", `<div class="activity-table">${runRows({ idPrefix: "overview-runs" })}${bulkActionBar()}</div>`, state.disclosures.recentActivity, `<button class="button compact" onclick="go('search')">New search <span aria-hidden="true">→</span></button>`)}`);
  }

  if (state.view === "history") {
    shell(`<div class="section-head page-heading"><div><p class="eyebrow section-kicker">ARCHIVE</p><h2>Run history</h2><p class="muted">Review, export, or remove individual discovery runs.</p></div><div class="actions">
      <a class="button ghost" href="/api/export?format=markdown">Run report</a>
      <a class="button ghost" href="/api/export?format=csv">Run CSV</a>
    </div></div><div class="section">${runRows({ idPrefix: "history-runs" })}${bulkActionBar()}</div>`);
  }

  if (state.view === "search") {
    shell(`<div class="card"><div class="section-head"><div><h2>Build a search</h2>
      <p class="muted">Search is executed by <code>gscrape.py</code>; results remain metadata-first.</p></div></div>
      <form id="search-form" class="form">
        <div class="field full target-account"><label>Search target — select one account</label><p class="muted">This search runs only against the selected mailbox. It will never search all configured accounts.</p><select name="account_id" required>
          <option value="">Choose one email account</option>${state.accounts.map((account) => `<option value="${account.id}" ${account.status !== "connected" ? "disabled" : ""}>${esc(account.display_name || account.label)} · ${esc(account.email)} — ${esc(account.status)}</option>`).join("")}
        </select></div>
        <div class="field full category-picker"><div class="section-head"><label>Discovery sections</label><div class="actions inline-actions"><button type="button" class="button ghost" onclick="setCategories(true)">Select all</button><button type="button" class="button ghost" onclick="setCategories(false)">Clear</button></div></div>
          <p class="muted">Choose one or more. Nothing is selected by default, so each scan only runs the sections you intend.</p>
          <div class="category-grid">${[
            ["gourl", "GoURL / service references"], ["wallet", "Crypto & wallets"],
            ["seed", "Recovery terms"], ["wallet-file", "Wallet files"],
            ["crypto-address", "Address-shaped candidates"], ["identity", "Identity variants"],
            ["phone", "Phone numbers"],
          ].map(([value, label]) => `<label class="category-option"><input type="checkbox" name="categories" value="${value}"><span>${label}</span></label>`).join("")}</div>
        </div>
        <div class="field full expert-options"><label>Expert regex overrides (optional)</label><p class="muted">These are local, case-insensitive additions to the selected detector. Use one pattern per section; leave blank to keep the safe default.</p>
          <div class="regex-grid">${[
            ["phone", "Phone format regex"], ["wallet-file", "Wallet filename regex"],
            ["seed", "Recovery cue/word regex"], ["crypto-address", "Crypto address regex"],
            ["gourl", "GoURL variant regex"], ["identity", "Identity variant regex"],
            ["wallet", "Wallet terminology regex"],
          ].map(([value, label]) => `<label>${label}<input class="category-regex" data-category="${value}" placeholder="Optional regex"></label>`).join("")}</div>
        </div>
        <div class="field"><label>Maximum messages</label><input name="max_messages" type="number" value="1000" min="1" max="10000"></div>
        <div class="field"><label>Maximum Gmail requests/second</label><input name="requests_per_second" type="number" value="1" min="0" max="10" step="0.1"><p class="muted">Local pacing ceiling. 0 disables proactive pacing; quota errors still use bounded adaptive backoff.</p></div>
        <div class="field"><label>Maximum adaptive cooldown (seconds)</label><input name="rate_limit_max_delay" type="number" value="60" min="1" max="300" step="1"><p class="muted">Upper bound after rate-limit responses.</p></div>
        <div class="field"><label>Gmail query (optional)</label><input name="query" placeholder="e.g. from:someone@example.com has:attachment"></div>
        <div class="field"><label>After (default: 2013/01/01)</label><input name="after_date" placeholder="YYYY/MM/DD"></div>
        <div class="field"><label>Before (default: 2018/12/31, inclusive)</label><input name="before_date" placeholder="YYYY/MM/DD"></div>
        <div class="field full"><label>Local profile note (optional)</label><textarea name="regex" placeholder="Saved for future local refinement; not sent to Gmail"></textarea></div>
        <div class="actions"><button class="button">Start search</button><button type="button" class="button ghost" onclick="saveProfile()">Save profile</button></div>
      </form></div>
      <div class="section card"><div class="section-head"><h2>Refined query suggestions</h2><select id="suggest-category" onchange="loadSuggestions()"><option value="all">All categories</option><option value="gourl">GoURL</option><option value="wallet">Wallet</option><option value="seed">Recovery</option><option value="wallet-file">Wallet files</option><option value="crypto-address">Addresses</option><option value="identity">Identity</option><option value="phone">Phone numbers</option></select></div><div id="suggestions" class="suggestions"><span class="muted">Choose a category to load suggestions.</span></div></div>`);
  }

  if (state.view === "console") {
    shell(`<div class="console" id="console">GScrape local console\nType: runs | report | export matches-csv | status\n\n${state.runs.map((run) => `[${run.status}] ${run.category} — ${run.matches || 0} matches`).join("\n")}</div>
      <div class="cmd"><input id="command" placeholder="Enter an approved command…"><button class="button" onclick="command()">Run</button></div>`);
  }

  if (state.view === "settings") {
    shell(`<div class="card"><div class="section-head"><div><h2>Email accounts</h2><p class="muted">OAuth sessions are saved locally with restrictive permissions.</p></div></div>
      <form id="account-form" class="form compact-form">
        <div class="field"><label>Display name</label><input name="name" required maxlength="100" placeholder="Personal Gmail"></div>
        <div class="field"><label>Email address</label><input name="email" type="email" required maxlength="254" placeholder="you@example.com"></div>
        <div class="field"><label>Thumbnail initials</label><input name="avatar" maxlength="3" placeholder="Optional"></div>
        <div class="field"><label>Identity preview</label><div class="account-preview"><span class="avatar" id="avatar-preview">?</span><span id="name-preview">New account</span></div></div>
        <div class="actions full"><button class="button">Add email account</button><button type="reset" class="button ghost">Clear</button></div>
      </form>
      ${state.accounts.length ? `<div class="table-wrap section"><table class="table"><thead><tr><th>Account</th><th>Email</th><th>Status</th><th>Actions</th></tr></thead><tbody>${state.accounts.map((account) => `<tr>
        <td><span class="avatar small">${esc(account.avatar)}</span> ${esc(account.display_name || account.label)}</td><td>${esc(account.email)}</td><td><span class="badge ${account.status === "connected" ? "green" : ""}">${esc(account.status)}</span></td>
        <td><span data-account-status="${account.id}" class="badge ${account.status === "connected" ? "green" : ""}">${esc(account.status)}</span>
        <button class="button ghost" onclick="authorize('${account.id}')">Authorize</button>
        <button class="button ghost" onclick="profileCheck('${account.id}')">Profile</button>
        <button class="button ghost" onclick="showLabels('${account.id}')">Labels</button>
        <button class="button ghost" onclick="removeAccount('${account.id}')">Remove</button></td>
      </tr>`).join("")}</tbody></table></div>` : '<div class="empty">No email accounts configured.</div>'}</div>
      <div class="card section"><div class="section-head"><div><h2>Logging & troubleshooting</h2><p class="muted">Dated local logs with size-based rolling. Secrets and OAuth values are not logged.</p></div><button class="button ghost" onclick="refreshLogging()">Refresh</button></div>
        <form id="logging-form" class="form compact-form">
          <div class="field"><label>Log level</label><select name="level"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select></div>
          <div class="field"><label>Max file size (MB)</label><input name="max_mb" type="number" min="1" max="50" value="${Math.round(state.logging.config.max_bytes / 1048576)}"></div>
          <div class="field"><label>Retained files</label><input name="backup_count" type="number" min="1" max="20" value="${state.logging.config.backup_count}"></div>
          <div class="actions full"><button class="button">Save logging settings</button></div>
        </form>
        <p class="muted">Log directory: <code>logs/</code> · Files: ${state.logging.files.length}</p>
        <pre class="log-tail">${esc((state.logging.tail || []).join("\n") || "No dashboard log entries yet.")}</pre>
      </div>
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
    shell(`<div class="section-head"><div><h2>System diagnostics</h2><p class="muted">Local-only checks for the dashboard, scanner, storage, and logging.</p></div><button class="button" onclick="refreshDebug()">Refresh checks</button></div>
      ${snapshot ? `<div class="card"><div class="debug-summary"><span class="badge ${snapshot.overall ? "green" : ""}">${snapshot.overall ? "Operational" : "Attention needed"}</span><span class="muted">${esc(new Date(snapshot.timestamp).toLocaleString())}</span></div><div class="debug-grid">${snapshot.checks.map((check) => `<div class="debug-check"><span class="badge ${check.ok ? "green" : ""}">${check.ok ? "PASS" : "FAIL"}</span><div><strong>${esc(check.name)}</strong><div class="muted">${esc(check.detail)}</div></div></div>`).join("")}</div></div><div class="card section"><h2>Recent dashboard log</h2><pre class="log-tail">${esc((snapshot.logging.tail || []).join("\n") || "No log entries.")}</pre></div>` : '<div class="card empty">Loading diagnostics…</div>'}`);
  }
}

async function loadSuggestions() {
  const category = $("#suggest-category").value;
  try {
    const payload = await api(`/api/suggestions?category=${encodeURIComponent(category)}`);
    $("#suggestions").innerHTML = Object.entries(payload.suggestions).flatMap(([name, items]) =>
      items.map((item) => `<button class="suggestion" onclick='useSuggestion(${JSON.stringify(item).replace(/'/g, "\\u0027")})'><strong>${esc(name)}</strong> ${esc(item)}</button>`),
    ).join("") || '<span class="muted">No suggestions available.</span>';
  } catch (error) { $("#suggestions").textContent = error.message; }
}

function useSuggestion(query) {
  const input = document.querySelector('input[name="query"]');
  if (input) input.value = query;
}

function setCategories(selected) {
  document.querySelectorAll('input[name="categories"]').forEach((input) => {
    input.checked = selected;
  });
}

function collectSearchConfig(form) {
  const data = Object.fromEntries(new FormData(form));
  data.categories = [...form.querySelectorAll('input[name="categories"]:checked')].map((input) => input.value);
  if (!data.categories.length) throw Error("Select at least one discovery section.");
  data.category = data.categories.length === 7 ? "all" : data.categories[0];
  data.category_regex = {};
  form.querySelectorAll(".category-regex").forEach((input) => {
    if (input.value.trim()) data.category_regex[input.dataset.category] = input.value.trim();
  });
  data.max_messages = Number(data.max_messages);
  data.requests_per_second = Number(data.requests_per_second);
  data.rate_limit_max_delay = Number(data.rate_limit_max_delay);
  return data;
}

async function addAccount(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try { await api("/api/accounts", { method: "POST", body: JSON.stringify(data) }); await load(); go("settings"); }
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

function go(view) { state.view = view; render(); if (view === "search") loadSuggestions(); if (view === "debug") refreshDebug(); }

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

document.querySelectorAll(".nav").forEach((nav) => nav.onclick = () => go(nav.dataset.view));
document.addEventListener("click", (event) => {
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
    return;
  }
  const bulkAction = event.target.closest("[data-bulk-action]");
  if (bulkAction) {
    const action = bulkAction.dataset.bulkAction;
    if (action === "delete") deleteSelectedRuns();
    else exportSelectedRuns(action);
  }
});
document.addEventListener("change", (event) => {
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
});
load().then(() => setInterval(() => {
  refreshAccounts().catch(() => {});
  if (state.view === "overview" || state.view === "history" || state.detail) {
    refreshRunState().catch(() => {});
  }
}, 1500))
  .catch((error) => shell(`<div class="card"><h2>Dashboard unavailable</h2><p class="muted">${esc(error.message)}</p></div>`));
