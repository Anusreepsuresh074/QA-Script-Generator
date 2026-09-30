/**
 * QA Script Generator – workflow dashboard
 */

const $ = (id) => document.getElementById(id);

function log(message, type = "") {
  const el = $("logArea");
  const line = document.createElement("div");
  line.className = type ? `log-${type}` : "";
  line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
  el.prepend(line);
}

function setLoading(loading) {
  ["runPhase1", "runPhase2", "runFull"].forEach((id) => {
    $(id).disabled = loading;
  });
}

function setStepState(step, state) {
  const el = $(`step${step}`);
  el.classList.remove("active", "done");
  if (state) el.classList.add(state);
}

function resetSteps() {
  [1, 2, 3].forEach((n) => setStepState(n, ""));
}

/** Bumped whenever the selected ticket changes so in-flight renders are ignored. */
window.__resultsEpoch = 0;
window.__activeTicketId = null;

function clearWorkflowResults(message) {
  window.__resultsEpoch = (window.__resultsEpoch || 0) + 1;
  resetSteps();

  $("resultPhase1").innerHTML =
    `<div class="empty-state">Run a workflow step to see results.</div>`;
  $("resultPhase2").innerHTML =
    `<div class="empty-state">Scenarios will appear here.</div>`;
  $("resultPhase3").innerHTML =
    `<div class="empty-state">Generated scripts will appear here.</div>`;
  $("resultTests").innerHTML =
    `<div class="empty-state">Run tests or generate a report from “Generated outputs” to see results.</div>`;

  window.__lastTestResult = null;

  const select = $("scriptFileSelect");
  if (select) {
    select.innerHTML = "";
    select.classList.add("hidden");
    select.onchange = null;
  }

  setActiveReportTicket(null, false);
  if (message) log(message, "info");
}

function isStaleEpoch(epoch) {
  return epoch !== window.__resultsEpoch;
}

/**
 * Point run/report actions at a ticket without wiping the Phase 1/2/3 panels —
 * used by "Run tests" / "Generate report" so browsing Generated Outputs never
 * clears results already on screen.
 */
function setActiveTicketOnly(ticketId) {
  const next = (ticketId || "").trim();
  $("ticketId").value = next;
  window.__activeTicketId = next || null;
  return next;
}

/**
 * Switch the active ticket and wipe previous run panels when the key changes.
 */
function selectTicket(ticketId, { silent = false } = {}) {
  const next = (ticketId || "").trim();
  const prev = (window.__activeTicketId || "").trim();
  $("ticketId").value = next;

  if (next === prev) {
    return next;
  }

  if (prev || next) {
    clearWorkflowResults(
      silent
        ? null
        : prev && next
          ? `Cleared previous results (${prev}) — selected ${next}`
          : next
            ? `Selected ${next}`
            : "Cleared workflow results"
    );
  } else {
    clearWorkflowResults();
  }

  window.__activeTicketId = next || null;
  return next;
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await resp.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!resp.ok) {
    const msg = data.detail || (typeof data === "string" ? data : resp.statusText);
    throw new Error(typeof msg === "object" ? JSON.stringify(msg) : msg);
  }
  return data;
}

const SPEC_URL_CONFIG = {
  rest:      { label: "Swagger / OpenAPI URL (optional)",    placeholder: "https://example.com/openapi.json" },
  graphql:   { label: "GraphQL schema URL or endpoint (optional)", placeholder: "https://example.com/graphql or schema.graphql" },
  soap:      { label: "WSDL URL (optional)",                 placeholder: "https://example.com/service.wsdl" },
  grpc:      { label: "Proto file URL (optional)",           placeholder: "https://example.com/service.proto" },
  websocket: { label: "AsyncAPI spec URL (optional)",        placeholder: "https://example.com/asyncapi.yaml" },
};

function updateSpecUrlLabel() {
  const type = $("apiType").value;
  const cfg = SPEC_URL_CONFIG[type] || SPEC_URL_CONFIG.rest;
  $("specUrlLabel").textContent = cfg.label;
  $("specUrl").placeholder = cfg.placeholder;
}

function buildPayload() {
  const ticket_id = $("ticketId").value.trim();
  if (!ticket_id) throw new Error("Enter a Jira ticket ID");

  const api_type = $("apiType").value || "rest";
  const framework = $("framework").value || "pytest";
  const payload = { ticket_id, api_type, framework };

  const specUrl = $("specUrl").value.trim();
  if (specUrl) {
    if (api_type === "rest") {
      payload.swagger_url = specUrl;
    } else {
      payload.spec_url = specUrl;
    }
  }

  const apiBase = $("apiBaseUrl").value.trim();
  if (apiBase) payload.api_base_url = apiBase;

  payload.attach_to_jira = $("attachJira").checked;
  payload.create_pr = $("createPr").checked;
  return payload;
}

function renderConfig(cfg) {
  const repo = cfg.github_automation_repo || "(not configured)";
  $("configCard").innerHTML = `
    <div style="font-size:0.85rem;line-height:1.7">
      <div><strong>Jira:</strong> ${escapeHtml(cfg.jira_url)}</div>
      <div><strong>Project:</strong> ${escapeHtml(cfg.jira_project_key)}</div>
      <div><strong>Mapping:</strong> ${escapeHtml(cfg.mapping_type)}</div>
      <div><strong>LLM:</strong> ${escapeHtml(cfg.ollama_model)} (Ollama → ${escapeHtml(cfg.groq_model)} fallback)</div>
      <div><strong>Output:</strong> ${escapeHtml(cfg.output_dir)}/</div>
      <div><strong>Default API:</strong> ${escapeHtml(cfg.api_base_url)}</div>
      <div><strong>Automation repo:</strong> ${escapeHtml(repo)}</div>
      <div><strong>PR base branch:</strong> ${escapeHtml(cfg.github_pr_base_branch || "main")}</div>
      <div><strong>Tests path:</strong> ${escapeHtml(cfg.github_tests_path || "tests")}/ <span style="color:var(--muted)">(fallback)</span></div>
    </div>`;
  if (!$("apiBaseUrl").value) $("apiBaseUrl").value = cfg.api_base_url;
  if (typeof cfg.auto_create_pr === "boolean") {
    $("createPr").checked = cfg.auto_create_pr;
  }

  // Seed the PR options from settings; the user can override any of them.
  window.__prBranchPrefix = cfg.github_pr_branch_prefix || "automation";
  if (typeof cfg.github_analyze_repo === "boolean") {
    $("prAnalyzeRepo").checked = cfg.github_analyze_repo;
  }
  if (typeof cfg.github_adapt_tests === "boolean") {
    $("prAdaptTests").checked = cfg.github_adapt_tests;
  }
  const pathInput = $("prTargetPath");
  if (pathInput && !pathInput.value) {
    pathInput.placeholder = cfg.github_tests_path || "tests";
  }
  if (!cfg.github_automation_repo) {
    const hint = $("prRepoHint");
    if (hint) hint.textContent = "GITHUB_AUTOMATION_REPO is not configured";
  }
  updatePrPlan();
}

function renderPhase1(data) {
  if (!data) return;
  const criteria = (data.acceptance_criteria || [])
    .map((c, i) => `<li>${i + 1}. ${escapeHtml(c)}</li>`)
    .join("");

  const endpointRows = (data.all_apis || data.apis || [])
    .map((a) => {
      const score = a.match_score != null ? `${a.match_score.toFixed(1)}%` : "—";
      const badge = a.matched
        ? `<span class="badge positive">matched</span>`
        : `<span class="badge edge">below TOP_P</span>`;
      return `<tr>
        <td><code>${escapeHtml(a.method?.toUpperCase())} ${escapeHtml(a.endpoint)}</code></td>
        <td>${score}</td>
        <td>${badge}</td>
      </tr>`;
    })
    .join("");

  const matrixRows = (data.match_scores || [])
    .slice(0, 120)
    .map(
      (row) => `<tr>
        <td title="${escapeHtml(row.criterion)}">${escapeHtml(truncate(row.criterion, 48))}</td>
        <td><code>${escapeHtml(row.method)} ${escapeHtml(row.endpoint)}</code></td>
        <td>${row.score.toFixed(1)}%</td>
        <td>${row.selected ? "✓" : ""}</td>
      </tr>`
    )
    .join("");

  const matchedApis = (data.apis || [])
    .map(
      (a) =>
        `<li><code>${escapeHtml(a.method?.toUpperCase())} ${escapeHtml(a.endpoint)}</code>` +
        (a.match_score != null ? ` — ${a.match_score.toFixed(1)}% match` : "") +
        `</li>`
    )
    .join("");

  $("resultPhase1").innerHTML = `
    <p><strong>${escapeHtml(data.ticket_id)}</strong> — ${escapeHtml(data.summary || "")}</p>
    <p style="margin:0.75rem 0 0.35rem;color:var(--muted);font-size:0.85rem">Acceptance criteria (${(data.acceptance_criteria || []).length})</p>
    <ul style="margin-left:1.2rem;font-size:0.85rem">${criteria || "<li>No criteria extracted</li>"}</ul>
    <p style="margin:0.75rem 0 0.35rem;color:var(--muted);font-size:0.85rem">API match probabilities (${(data.all_apis || data.apis || []).length})</p>
    <table class="scenario-table" style="margin-bottom:0.75rem">
      <thead><tr><th>Endpoint</th><th>Best score</th><th>Status</th></tr></thead>
      <tbody>${endpointRows || '<tr><td colspan="3">None — add Swagger URL and re-run</td></tr>'}</tbody>
    </table>
    <p style="margin:0.75rem 0 0.35rem;color:var(--muted);font-size:0.85rem">Mapped APIs (${(data.apis || []).length})</p>
    <ul style="margin-left:1.2rem;font-size:0.85rem">${matchedApis || "<li>None above TOP_P threshold</li>"}</ul>
    ${
      matrixRows
        ? `<details style="margin-top:0.75rem">
            <summary style="cursor:pointer;color:var(--muted);font-size:0.85rem">Per-criterion scores (${(data.match_scores || []).length})</summary>
            <table class="scenario-table" style="margin-top:0.5rem">
              <thead><tr><th>Criterion</th><th>Endpoint</th><th>Score</th><th>Selected</th></tr></thead>
              <tbody>${matrixRows}</tbody>
            </table>
          </details>`
        : ""
    }`;
  setStepState(1, "done");
}

function truncate(text, maxLen) {
  if (!text || text.length <= maxLen) return text || "";
  return `${text.slice(0, maxLen - 1)}…`;
}

function renderPhase2(data) {
  if (!data || !data.scenarios) return;
  const rows = data.scenarios
    .map(
      (s, i) => `<tr>
        <td>${i + 1}</td>
        <td><span class="badge ${s.type}">${s.type}</span></td>
        <td>${escapeHtml(s.name)}</td>
        <td>${s.method ? escapeHtml(s.method) : ""} ${s.api_endpoint ? escapeHtml(s.api_endpoint) : "—"}</td>
      </tr>`
    )
    .join("");

  const pos = data.scenarios.filter((s) => s.type === "positive").length;
  const neg = data.scenarios.filter((s) => s.type === "negative").length;
  const edge = data.scenarios.filter((s) => s.type === "edge").length;

  $("resultPhase2").innerHTML = `
    <p style="margin-bottom:0.75rem">${data.scenarios.length} scenarios
      <span class="badge positive">${pos} +</span>
      <span class="badge negative">${neg} −</span>
      <span class="badge edge">${edge} ~</span>
    </p>
    <table class="scenario-table">
      <thead><tr><th>#</th><th>Type</th><th>Name</th><th>API</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  setStepState(2, "done");
}

const FRAMEWORK_LABELS = {
  pytest:  "pytest (Python)",
  robot:   "Robot Framework",
  jest:    "Jest (JavaScript)",
  postman: "Postman / Newman",
};

// Extensions considered "code files" worth previewing (excludes config stubs)
const PREVIEW_EXTENSIONS = [".py", ".robot", ".test.js", ".json", ".js"];

function renderPhase3(data) {
  if (!data || !data.files) return;

  const framework = data.framework || "pytest";
  const fwLabel = FRAMEWORK_LABELS[framework] || framework;

  // Show all previewable files in the selector
  const previewFiles = data.files.filter((f) =>
    PREVIEW_EXTENSIONS.some((ext) => f.filename.endsWith(ext))
  );

  const select = $("scriptFileSelect");
  select.innerHTML = "";
  previewFiles.forEach((f) => {
    const opt = document.createElement("option");
    opt.value = f.filename;
    opt.textContent = f.filename;
    select.appendChild(opt);
  });
  select.classList.toggle("hidden", previewFiles.length <= 1);

  const showFile = (filename) => {
    const file = data.files.find((f) => f.filename === filename);
    if (file) {
      $("resultPhase3").innerHTML = `<pre>${escapeHtml(file.content)}</pre>`;
    }
  };

  if (previewFiles.length) {
    showFile(previewFiles[0].filename);
    select.onchange = () => showFile(select.value);
  } else {
    $("resultPhase3").innerHTML = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
  }

  setStepState(3, "done");
  log(`[${fwLabel}] Scripts written to ${data.output_dir}`, "ok");
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function loadHealth() {
  try {
    const h = await api("/api/dashboard/health-detail");
    const ok = h.api === "ok" && h.jira === "ok";
    $("statusDot").className = `status-dot ${ok ? "ok" : "error"}`;
    $("statusText").textContent = ok ? "API & Jira OK" : `Jira: ${h.jira}`;
  } catch {
    $("statusDot").className = "status-dot error";
    $("statusText").textContent = "Offline";
  }
}

async function loadConfig() {
  const cfg = await api("/api/dashboard/config");
  window.__dashboardConfig = cfg;
  renderConfig(cfg);
}

async function loadTickets(projectKey) {
  const list = $("ticketList");
  list.innerHTML = `<li class="empty-state" style="padding:0.5rem">Loading…</li>`;
  try {
    const project = projectKey || window.__dashboardConfig?.jira_project_key || "QA";
    const tickets = await api(`/api/dashboard/tickets?project=${encodeURIComponent(project)}`);
    list.innerHTML = "";
    if (!tickets.length) {
      list.innerHTML = `<li class="empty-state" style="padding:0.5rem">No tickets found in project ${escapeHtml(project)}</li>`;
      return;
    }
    tickets.forEach((t) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="ticket-key">${escapeHtml(t.key)}</span>
        <div>${escapeHtml(t.summary)}</div>
        <div class="ticket-meta">${escapeHtml(t.status)} · ${escapeHtml(t.issue_type)}</div>`;
      li.onclick = () => {
        document.querySelectorAll(".ticket-list li").forEach((el) => el.classList.remove("selected"));
        li.classList.add("selected");
        const key = selectTicket(t.key);
        checkReportStatus(key);
      };
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = `<li class="empty-state" style="padding:0.5rem;color:var(--error)">${escapeHtml(e.message)}</li>`;
  }
}

async function loadOutputs() {
  const list = $("outputList");
  try {
    const outputs = await api("/api/dashboard/outputs");
    list.innerHTML = "";
    renderPrTicketOptions();
    if (!outputs.length) {
      list.innerHTML = `<li class="empty-state" style="padding:0.5rem">No outputs yet</li>`;
      return;
    }
    outputs.forEach((o) => {
      const li = document.createElement("li");
      li.className = "output-item";
      const chips = o.files
        .map(
          (f) =>
            `<span class="file-chip" data-ticket="${escapeHtml(o.ticket_id)}" data-file="${escapeHtml(f.filename)}">${escapeHtml(f.filename)}</span>`
        )
        .join("");
      const hasTests = o.files.some((f) => f.filename.startsWith("test_") && f.filename.endsWith(".py"));
      const runBtn = hasTests
        ? `<button type="button" class="btn-ghost run-tests-btn" data-ticket="${escapeHtml(o.ticket_id)}">▶ Run tests</button>`
        : "";
      const reportBtn = hasTests
        ? `<button type="button" class="btn-ghost generate-report-btn" data-ticket="${escapeHtml(o.ticket_id)}">📄 Generate report</button>`
        : "";
      const prBtn = hasTests
        ? `<button type="button" class="btn-ghost raise-pr-btn" data-ticket="${escapeHtml(o.ticket_id)}" title="Raise a PR with all generated tests (no re-run)">🔀 Raise PR</button>`
        : "";
      // Scripts only in Generated outputs — reports live under reports/ and Test run results panel
      li.innerHTML = `<div class="output-item-head"><strong>${escapeHtml(o.ticket_id)}</strong><span class="output-item-btns">${runBtn}${reportBtn}${prBtn}</span></div>${chips}`;
      list.appendChild(li);
    });
    list.querySelectorAll(".run-tests-btn").forEach((btn) => {
      btn.onclick = () => {
        setActiveTicketOnly(btn.dataset.ticket);
        setActiveReportTicket(btn.dataset.ticket);
        runTests(btn.dataset.ticket, btn);
      };
    });
    list.querySelectorAll(".generate-report-btn").forEach((btn) => {
      btn.onclick = () => {
        setActiveTicketOnly(btn.dataset.ticket);
        setActiveReportTicket(btn.dataset.ticket);
        generateReport(btn.dataset.ticket, btn);
      };
    });
    list.querySelectorAll(".raise-pr-btn").forEach((btn) => {
      btn.onclick = () => {
        setActiveTicketOnly(btn.dataset.ticket);
        raisePr(btn.dataset.ticket, btn);
      };
    });
    list.querySelectorAll(".file-chip").forEach((chip) => {
      chip.onclick = async () => {
        const ticketId = selectTicket(chip.dataset.ticket);
        const filename = chip.dataset.file;
        try {
          const file = await api(`/api/dashboard/outputs/${ticketId}/${filename}`);
          if (ticketId !== window.__activeTicketId) return;
          $("resultPhase3").innerHTML = `<pre>${escapeHtml(file.content)}</pre>`;
          setStepState(3, "done");
          log(`Preview ${ticketId}/${filename}`, "info");
        } catch (e) {
          log(e.message, "err");
        }
      };
    });
  } catch (e) {
    list.innerHTML = `<li class="empty-state" style="padding:0.5rem">${escapeHtml(e.message)}</li>`;
  }
}

function pct(count, total) {
  return total ? Math.round((count / total) * 1000) / 10 : 0;
}

function buildPieChartStyle(passed, failed, errors, skipped) {
  const total = passed + failed + errors + skipped;
  if (!total) return "background:#e7ecf3";
  let angle = 0;
  const parts = [
    [passed, "#22c55e"],
    [failed, "#ef4444"],
    [errors, "#dc2626"],
    [skipped, "#a855f7"],
  ]
    .filter(([value]) => value > 0)
    .map(([value, color]) => {
      const start = angle;
      angle += (value / total) * 100;
      return `${color} ${start}% ${angle}%`;
    });
  return `background:conic-gradient(${parts.join(", ")})`;
}

function buildTestResultsOverview(data) {
  const { passed, failed, errors, skipped, total } = data;
  const failedTotal = failed + errors;
  const passRate = pct(passed, total);
  const failRate = pct(failedTotal, total);
  const passRateClass = passRate >= 80 ? "positive" : passRate >= 50 ? "edge" : "negative";

  const legendItems = [
    ["Passed", passed, "#22c55e"],
    ["Failed", failed, "#ef4444"],
    ["Errors", errors, "#dc2626"],
    ["Skipped", skipped, "#a855f7"],
  ]
    .map(
      ([label, count, color]) => `<li>
        <span class="legend-dot" style="background:${color}"></span>
        <span class="legend-label">${label}</span>
        <span class="legend-count">${count}</span>
        <span class="legend-pct">${pct(count, total)}%</span>
      </li>`
    )
    .join("");

  return `
    <div class="test-results-overview">
      <div class="test-pie" style="${buildPieChartStyle(passed, failed, errors, skipped)}" role="img" aria-label="Test results pie chart"></div>
      <div class="test-breakdown">
        <p class="test-pass-rate ${passRateClass}">${passRate}% pass rate</p>
        <p><strong>${passed}</strong> of <strong>${total}</strong> test cases passed.</p>
        <p><strong class="negative-text">${failedTotal}</strong> failed or errored (${failRate}%).</p>
        ${skipped ? `<p><strong class="edge-text">${skipped}</strong> skipped (${pct(skipped, total)}%).</p>` : ""}
      </div>
      <ul class="test-legend">${legendItems}</ul>
    </div>`;
}

function prLink(url, label) {
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label || url)}</a>`;
}

/** "branch → path" detail line, so the PR's destination is visible at a glance. */
function prDestination(pr) {
  if (!pr) return "";
  const bits = [];
  if (pr.branch) bits.push(`branch <code>${escapeHtml(pr.branch)}</code>`);
  bits.push(`<code>${escapeHtml(pr.target_path || "(repo root)")}/</code>`);
  if (pr.file_count) bits.push(`${pr.file_count} file(s)`);
  if (pr.adapted_files?.length) {
    bits.push(`${pr.adapted_files.length} module(s) adapted to the repo`);
  }
  return `<div class="pr-note">${bits.join(" → ")}</div>`;
}

function renderPrBanner(data) {
  const prs = data.prs || [];
  const failures = data.failures || [];

  if (prs.length > 1) {
    const items = prs
      .map(
        (pr) =>
          `<li>${prLink(pr.pr_url, pr.tests?.length ? pr.tests.join(", ") : pr.label)}
            <span style="color:var(--muted)"> — ${escapeHtml(pr.created ? "opened" : "updated")}, ${pr.file_count} file(s)</span>
          </li>`
      )
      .join("");
    const notes = prs.flatMap((pr) => pr.notes || []);
    return `<div class="pr-banner">
      <strong>${prs.length} pull requests ready</strong>
      <ul class="pr-list">${items}</ul>
      ${prDestination({ target_path: prs[0].target_path, file_count: 0, adapted_files: prs.flatMap((pr) => pr.adapted_files || []) })}
      ${data.pr_message ? `<div style="margin-top:0.35rem;color:var(--muted)">${escapeHtml(data.pr_message)}</div>` : ""}
      ${notes.length ? `<div class="pr-note">${notes.map(escapeHtml).join("<br>")}</div>` : ""}
      ${failures.length ? `<div class="pr-note warn-text">${failures.map(escapeHtml).join("<br>")}</div>` : ""}
    </div>`;
  }

  if (data.pr_url) {
    const single = prs[0];
    const notes = single?.notes || [];
    return `<div class="pr-banner">
      Pull request ready${single?.label ? ` (${escapeHtml(single.label)})` : ""}:
      ${prLink(data.pr_url)}
      ${prDestination(single)}
      ${data.pr_message ? `<div style="margin-top:0.35rem;color:var(--muted)">${escapeHtml(data.pr_message)}</div>` : ""}
      ${notes.length ? `<div class="pr-note">${notes.map(escapeHtml).join("<br>")}</div>` : ""}
      ${failures.length ? `<div class="pr-note warn-text">${failures.map(escapeHtml).join("<br>")}</div>` : ""}
    </div>`;
  }

  if (data.pr_message) {
    return `<div class="pr-banner warn">${escapeHtml(data.pr_message)}</div>`;
  }
  return "";
}

/**
 * Replace the PR banner in place after a manual "Raise PR", keeping results on
 * screen. With no run rendered yet, the banner becomes the panel content so the
 * PR link is never lost to the activity log alone.
 */
function updatePrBanner(data) {
  const banner = renderPrBanner(data);
  const slot = $("prBannerSlot");
  if (slot) {
    slot.innerHTML = banner;
  } else if (banner) {
    $("resultTests").innerHTML = `${banner}
      <div class="empty-state">Run tests to see per-case results for this ticket.</div>`;
  }
}

/** Counters of the run currently on screen, sent along so the PR body quotes them. */
function lastRunCounters(ticketId) {
  const last = window.__lastTestResult;
  if (!last || last.ticket_id !== ticketId) return {};
  return {
    total: last.total,
    passed: last.passed,
    failed: last.failed,
    errors: last.errors,
    skipped: last.skipped,
    duration: last.duration,
    base_url: last.base_url,
    cases: (last.cases || []).map((c) => ({
      name: c.name,
      classname: c.classname || "",
      outcome: c.outcome,
    })),
  };
}

/**
 * Preview of what the backend will make of a typed branch name.
 *
 * Mirrors `sanitize_branch` in app/github/pr_service.py so the plan line shows
 * the ref that will actually be created, not the raw text. The server remains
 * the authority — this is display only.
 */
function previewBranchName(name) {
  let out = (name || "").trim().replace(/[^A-Za-z0-9._/-]+/g, "-");
  out = out.replace(/\.{2,}/g, ".").replace(/\/{2,}/g, "/").replace(/-{2,}/g, "-");
  out = out
    .split("/")
    .map((part) => part.replace(/^[.-]+|[.-]+$/g, ""))
    .filter(Boolean)
    .join("/");
  if (out.endsWith(".lock")) out = out.slice(0, -5);
  return out.slice(0, 200).replace(/^[/\-.]+|[/\-.]+$/g, "");
}

/** Which tests a PR carries, from the PR options block: all | passed. */
function selectedPrScope() {
  return $("prScopeSelect")?.value || "all";
}

/** How the selection is split into PRs: single | per_ticket | per_test. */
function selectedPrGrouping() {
  return $("prGrouping")?.value || "single";
}

/** Tickets ticked in the PR options block, active ticket first. */
function selectedPrTickets(activeTicket) {
  const ticked = Array.from(
    document.querySelectorAll("#prTicketList input[type=checkbox]:checked")
  ).map((el) => el.value);
  const ordered = activeTicket ? [activeTicket] : [];
  ticked.forEach((t) => {
    if (!ordered.includes(t)) ordered.push(t);
  });
  return ordered;
}

/**
 * Every user-set PR control, as the API field names.
 *
 * A blank input means "not specified" and is left out of the payload so the
 * backend applies its own default — an auto-generated branch, the test root it
 * discovered in the target repo, or the generated title.
 */
function prControlValues(activeTicket) {
  const branch = $("prBranch")?.value.trim() || "";
  const targetPath = $("prTargetPath")?.value.trim() || "";
  const title = $("prTitle")?.value.trim() || "";
  const values = {
    scope: selectedPrScope(),
    grouping: selectedPrGrouping(),
    analyze_repo: Boolean($("prAnalyzeRepo")?.checked),
    adapt_tests: Boolean($("prAdaptTests")?.checked),
  };
  if (branch) values.branch = branch;
  if (targetPath) values.target_path = targetPath;
  if (title) values.title = title;

  const tickets = selectedPrTickets(activeTicket).filter((t) => t !== activeTicket);
  if (tickets.length) values.tickets = tickets;
  return values;
}

/** The same controls, named for the run/report endpoints' pr_* fields. */
function runPrControlValues(activeTicket) {
  const v = prControlValues(activeTicket);
  const payload = {
    create_pr: $("createPr").checked,
    pr_scope: v.scope,
    pr_grouping: v.grouping,
    pr_analyze_repo: v.analyze_repo,
    pr_adapt_tests: v.adapt_tests,
  };
  if (v.branch) payload.pr_branch = v.branch;
  if (v.target_path) payload.pr_target_path = v.target_path;
  if (v.title) payload.pr_title = v.title;
  return payload;
}

/**
 * A checkbox per ticket that has generated tests, so one PR can span tickets.
 *
 * Reads /pr/tickets rather than /outputs — the latter deliberately reports only
 * the most recent run, which would leave nothing to combine.
 */
async function renderPrTicketOptions() {
  const box = $("prTicketList");
  if (!box) return;
  let tickets = [];
  try {
    tickets = await api("/api/dashboard/pr/tickets");
  } catch (e) {
    box.innerHTML = `<span class="hint">Could not list tickets: ${escapeHtml(e.message)}</span>`;
    return;
  }
  if (!tickets.length) {
    box.innerHTML = `<span class="hint">No generated tests yet.</span>`;
    return;
  }
  const previously = new Set(selectedPrTickets(null));
  box.innerHTML = tickets
    .map((t) => {
      const id = `prTicket-${t.ticket_id.replace(/[^A-Za-z0-9_-]/g, "-")}`;
      const checked = previously.has(t.ticket_id) ? " checked" : "";
      const count = t.test_files === 1 ? "1 module" : `${t.test_files} modules`;
      return `<label for="${id}" title="${escapeHtml(count)}"><input type="checkbox" id="${id}" value="${escapeHtml(t.ticket_id)}"${checked} />${escapeHtml(t.ticket_id)}</label>`;
    })
    .join("");
  box.querySelectorAll("input[type=checkbox]").forEach((el) => {
    el.onchange = updatePrPlan;
  });
  syncPrTicketSelection();
}

/**
 * Keep the active ticket ticked and non-removable — it is the ticket the PR is
 * raised for, so unticking it would be a no-op that reads as a real choice.
 */
function syncPrTicketSelection() {
  const active = window.__activeReportTicket || $("ticketId").value.trim();
  document.querySelectorAll("#prTicketList input[type=checkbox]").forEach((el) => {
    const isActive = el.value === active;
    el.disabled = isActive;
    if (isActive) el.checked = true;
    el.parentElement.title = isActive
      ? "The ticket currently on screen is always included"
      : "Include this ticket's generated tests in the PR";
  });
  updatePrPlan();
}

/** Count of PRs the current settings would raise, and the branch they land on. */
function prPlanPreview() {
  const active = window.__activeReportTicket || $("ticketId").value.trim();
  const grouping = selectedPrGrouping();
  const tickets = active ? selectedPrTickets(active) : [];
  const branch = $("prBranch")?.value.trim();
  const path = $("prTargetPath")?.value.trim();
  const scope = selectedPrScope();

  let cases = lastRunCases(active);
  if (scope === "passed") cases = cases.filter((c) => c.outcome === "passed");

  let count = 1;
  if (grouping === "per_ticket") count = Math.max(tickets.length, 1);
  if (grouping === "per_test") count = cases.length;

  return { active, grouping, tickets, branch, path, scope, count, cases };
}

/** One line under the controls saying exactly what "Raise PR" will do. */
function updatePrPlan() {
  const el = $("prPlan");
  if (!el) return;
  const plan = prPlanPreview();
  if (!plan.active) {
    el.innerHTML = `<span class="hint">Select a ticket with generated tests.</span>`;
    return;
  }

  const branch = plan.branch
    ? previewBranchName(plan.branch)
    : `automation/${plan.active}`;
  const branchNote = plan.branch ? "" : " (auto)";
  const path = plan.path
    ? `${plan.path}/`
    : window.__repoConventions?.suggested_path
      ? `${window.__repoConventions.suggested_path}/ (from repo)`
      : "detected from the repo";
  const scopeText = plan.scope === "passed" ? "passing tests only" : "all generated tests";

  let plural;
  if (plan.grouping === "per_test") {
    // Only the ticket on screen has its cases here; the others are resolved
    // from their saved reports server-side, so the count is a lower bound.
    const others =
      plan.tickets.length > 1
        ? `, plus any recorded tests in ${plan.tickets.length - 1} other ticket(s)`
        : "";
    plural = plan.count
      ? `<strong>${plan.count} PR(s)</strong>, one per test${others}`
      : `<span class="pr-plan-warn">no per-test results yet — run the tests first</span>`;
  } else if (plan.grouping === "per_ticket") {
    plural = `<strong>${plan.count} PR(s)</strong>, one per ticket`;
  } else {
    plural = `<strong>1 PR</strong> combining ${plan.tickets.length} ticket(s)`;
  }

  const over =
    plan.grouping === "per_test" && plan.count > 20
      ? ` <span class="pr-plan-warn">— over the 20 per-test PR limit</span>`
      : "";
  const adapt = $("prAdaptTests")?.checked
    ? " · tests rewritten to the repo's conventions"
    : "";

  el.innerHTML =
    `→ ${plural}${over} · ${escapeHtml(plan.tickets.join(", "))} · ${scopeText}<br />` +
    `&nbsp;&nbsp;branch <strong>${escapeHtml(branch)}</strong>${branchNote} → ${escapeHtml(path)}${adapt}`;
}

/**
 * Read the target repo and show what it found: where its tests live, which
 * fixtures exist, how modules are named. Also pre-fills the target path so the
 * detected location is visible and editable before anything is pushed.
 */
async function inspectTargetRepo(btn) {
  const original = btn ? btn.textContent : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Reading repo…";
  }
  const hint = $("prRepoHint");
  const box = $("prConventions");
  try {
    const data = await api("/api/dashboard/github/conventions?refresh=true");
    window.__repoConventions = data;
    if (hint) hint.textContent = `${data.repo}@${data.ref}`;
    if (box) {
      box.hidden = false;
      box.innerHTML =
        `<div class="pr-conventions-title">${escapeHtml(data.repo)}@${escapeHtml(data.ref)}</div>` +
        `<ul>${(data.summary || []).map((l) => `<li>${mdInline(l)}</li>`).join("")}</ul>`;
    }
    const pathInput = $("prTargetPath");
    if (pathInput) {
      pathInput.placeholder = data.suggested_path || "tests";
    }
    log(
      data.analyzed
        ? `Read ${data.repo}: ${data.test_file_count} test file(s), root “${data.test_root || "(repo root)"}”, ${(data.fixtures || []).length} fixture(s)`
        : `Could not analyze ${data.repo}: ${(data.notes || []).join("; ")}`,
      data.analyzed ? "ok" : "err"
    );
    updatePrPlan();
  } catch (e) {
    if (box) {
      box.hidden = false;
      box.innerHTML = `<span style="color:var(--error)">${escapeHtml(e.message)}</span>`;
    }
    log(`Target repo inspection failed: ${e.message}`, "err");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

/** Render the `backtick` spans the analyzer emits, escaping everything else. */
function mdInline(text) {
  return escapeHtml(String(text || "")).replace(
    /`([^`]+)`/g,
    (_, code) => `<code>${code}</code>`
  );
}

/** Per-test cases of the run on screen, or [] when nothing has been run yet. */
function lastRunCases(ticketId) {
  const last = window.__lastTestResult;
  if (!last || last.ticket_id !== ticketId) return [];
  return last.cases || [];
}

/** Backend test id for a case: module[::Class]::test_name, params stripped. */
function testIdOf(c) {
  const func = (c.name || "").split("[")[0];
  const parts = (c.classname || "").split(".").filter(Boolean);
  return [...parts, func].join("::");
}

/**
 * Confirm before a click opens more than one PR — the fan-out is outward-facing
 * and hard to undo, so the count and destination are spelled out first.
 */
function confirmPrPlan(ticketId, payload, tests) {
  if (tests && tests.length === 1) return true;

  const grouping = payload.grouping;
  const tickets = [ticketId, ...(payload.tickets || [])];
  let count = 1;
  if (grouping === "per_test") {
    let cases = lastRunCases(ticketId);
    if (payload.scope === "passed") cases = cases.filter((c) => c.outcome === "passed");
    count = tests ? tests.length : cases.length;
  } else if (grouping === "per_ticket") {
    count = tickets.length;
  }
  if (count <= 1) return true;

  const what = grouping === "per_test" ? "one per test" : "one per ticket";
  const atLeast = grouping === "per_test" && tickets.length > 1 ? "at least " : "";
  const branch = payload.branch
    ? `Branches start from “${previewBranchName(payload.branch)}” with a distinguishing suffix.`
    : "Each PR gets its own branch so they can be merged independently.";
  return window.confirm(
    `Raise ${atLeast}${count} separate pull request(s) — ${what}?\n\n` +
      `Tickets: ${tickets.join(", ")}\n${branch}`
  );
}

function renderTestResults(data, reportMeta = null) {
  const ok = data.failed === 0 && data.errors === 0 && data.total > 0;
  const verdict = data.total === 0
    ? `<span class="badge edge">no tests collected</span>`
    : ok
      ? `<span class="badge positive">all passed</span>`
      : `<span class="badge negative">failing</span>`;

  const rows = data.cases
    .map((c) => {
      const cls = { passed: "positive", failed: "negative", error: "negative", skipped: "edge" }[c.outcome] || "edge";
      const msg = c.message
        ? `<div class="test-msg">${escapeHtml(truncate(c.message.replace(/\s+/g, " "), 160))}</div>`
        : "";
      const testId = testIdOf(c);
      return `<tr>
        <td><span class="badge ${cls}">${c.outcome}</span></td>
        <td><code>${escapeHtml(c.name)}</code>${msg}</td>
        <td>${c.duration.toFixed(2)}s</td>
        <td><button type="button" class="btn-ghost btn-xs row-pr-btn"
              data-ticket="${escapeHtml(data.ticket_id)}" data-test="${escapeHtml(testId)}"
              title="Raise a PR for just this test">🔀 PR</button></td>
      </tr>`;
    })
    .join("");

  $("resultTests").innerHTML = `
    <p style="margin-bottom:0.75rem">
      ${verdict}
      <span class="badge positive">${data.passed} passed</span>
      <span class="badge negative">${data.failed + data.errors} failed</span>
      ${data.skipped ? `<span class="badge edge">${data.skipped} skipped</span>` : ""}
      <span style="color:var(--muted);font-size:0.8rem;margin-left:0.4rem">${data.duration.toFixed(2)}s · ${escapeHtml(data.base_url)}</span>
    </p>
    ${buildTestResultsOverview(data)}
    <div id="prBannerSlot">${renderPrBanner(data)}</div>
    <h3 class="test-details-heading">Test case details</h3>
    <table class="scenario-table">
      <thead><tr><th>Result</th><th>Test</th><th>Time</th><th>PR</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4">No test cases reported</td></tr>'}</tbody>
    </table>
    ${
      reportMeta
        ? `<p style="margin-top:0.75rem;font-size:0.85rem;color:var(--muted)">
            Report saved ${escapeHtml(reportMeta.generated_at || "")} —
            <a href="/api/dashboard/outputs/${encodeURIComponent(data.ticket_id)}/report/download?format=html" download>HTML</a> ·
            <a href="/api/dashboard/outputs/${encodeURIComponent(data.ticket_id)}/report/download?format=xlsx" download>Excel</a> ·
            <a href="/api/dashboard/outputs/${encodeURIComponent(data.ticket_id)}/report/download?format=json" download>JSON</a> ·
            <a href="/api/dashboard/outputs/${encodeURIComponent(data.ticket_id)}/report/download?format=csv" download>CSV</a>
          </p>`
        : ""
    }
    <details style="margin-top:0.75rem">
      <summary style="cursor:pointer;color:var(--muted);font-size:0.85rem">Raw pytest output (exit ${data.exit_code})</summary>
      <pre style="margin-top:0.5rem">${escapeHtml(data.output || "(no output)")}</pre>
    </details>`;
  window.__lastTestResult = data;

  // Per-test "PR" buttons: one pull request for that single test
  $("resultTests")
    .querySelectorAll(".row-pr-btn")
    .forEach((btn) => {
      btn.onclick = () =>
        raisePr(btn.dataset.ticket, btn, {
          scope: "all",
          grouping: "per_test",
          tests: [btn.dataset.test],
        });
    });

  setActiveReportTicket(data.ticket_id, Boolean(reportMeta));
}

function setActiveReportTicket(ticketId, hasReport = false) {
  window.__activeReportTicket = ticketId || null;
  window.__hasTestReport = hasReport;
  const genBtn = $("generateReportBtn");
  const dlBtn = $("downloadReportBtn");
  const fmt = $("reportFormatSelect");
  const prBtn = $("raisePrBtn");
  const enabled = Boolean(ticketId);
  genBtn.disabled = !enabled;
  fmt.disabled = !enabled;
  dlBtn.disabled = !enabled || !hasReport;
  if (prBtn) {
    prBtn.disabled = !enabled;
    prBtn.title = enabled
      ? `Raise pull request(s) for ${ticketId} using the selected PR scope`
      : "Select a ticket with generated tests";
  }
  // The PR options stay enabled — they also drive the automatic post-run PR.
  syncPrTicketSelection();
}

const PR_SCOPE_LABELS = {
  all: "all generated tests",
  passed: "passing tests only",
  individual: "one PR per test",
};

const PR_GROUPING_LABELS = {
  single: "one combined PR",
  per_ticket: "one PR per ticket",
  per_test: "one PR per test",
};

/**
 * Raise PR(s) for a ticket's generated tests without re-running them — the
 * "review the results, then ship what looks right" path.
 *
 * `scope` is all | passed | individual; `tests` narrows an individual scope to
 * specific test ids (used by the per-row buttons).
 */
async function raisePr(ticketId, btn, { scope, grouping, tests } = {}) {
  const payload = {
    ...lastRunCounters(ticketId),
    ...prControlValues(ticketId),
  };
  // The per-row "PR" button overrides the panel: just this one test.
  if (scope) payload.scope = scope;
  if (grouping) payload.grouping = grouping;
  if (tests?.length) {
    payload.tests = tests;
    delete payload.tickets;
  }

  if (!confirmPrPlan(ticketId, payload, tests)) {
    log("PR cancelled", "info");
    return;
  }

  const original = btn ? btn.textContent : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Raising PR…";
  }
  const what =
    tests?.length === 1
      ? tests[0]
      : `${PR_GROUPING_LABELS[payload.grouping] || payload.grouping}, ${PR_SCOPE_LABELS[payload.scope] || payload.scope}`;
  log(`Raising PR for ${[ticketId, ...(payload.tickets || [])].join(", ")} (${what})…`, "info");

  try {
    const data = await api(`/api/dashboard/outputs/${encodeURIComponent(ticketId)}/pr`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    updatePrBanner(data);

    const prs = data.prs || [];
    if (prs.length > 1) {
      log(`${prs.length} PRs raised on ${data.repo} — ${data.pr_message}`, "ok");
      prs.forEach((pr) => log(`  ${pr.tests?.join(", ") || pr.label}: ${pr.pr_url}`, "ok"));
    } else {
      log(
        `${data.created ? "PR opened" : "PR updated"} on ${data.repo}@${data.branch}: ${data.pr_url}`,
        "ok"
      );
    }
    if (data.target_path !== undefined) {
      log(`Files written to ${data.target_path || "(repo root)"}/ in ${data.repo}`, "info");
    }
    (data.conventions || []).forEach((c) => log(`  repo: ${c}`, "info"));
    prs.flatMap((pr) => pr.adapted_files || []).forEach((f) =>
      log(`  adapted to repo conventions: ${f}`, "ok")
    );
    (data.skipped || []).forEach((sk) => log(`skipped ${sk}`, "info"));
    (data.failures || []).forEach((f) => log(f, "err"));
    prs.flatMap((pr) => pr.notes || []).forEach((n) => log(n, "info"));
  } catch (e) {
    updatePrBanner({ pr_message: `PR creation failed: ${e.message}` });
    log(`Raise PR failed for ${ticketId}: ${e.message}`, "err");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

function downloadReport(ticketId, format) {
  const fmt = format || $("reportFormatSelect").value || "html";
  const url = `/api/dashboard/outputs/${encodeURIComponent(ticketId)}/report/download?format=${encodeURIComponent(fmt)}`;
  window.open(url, "_blank");
  log(`Downloading ${ticketId} test report (${fmt.toUpperCase()})`, "info");
}

async function checkReportStatus(ticketId) {
  try {
    const status = await api(`/api/dashboard/outputs/${encodeURIComponent(ticketId)}/report/status`);
    const hasReport = Object.values(status.available || {}).some(Boolean);
    setActiveReportTicket(ticketId, hasReport);
    return hasReport;
  } catch {
    setActiveReportTicket(ticketId, false);
    return false;
  }
}

async function generateReport(ticketId, btn) {
  const original = btn ? btn.textContent : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Generating…";
  }
  const epoch = window.__resultsEpoch;
  setActiveReportTicket(ticketId);
  $("resultTests").innerHTML = `<div class="empty-state"><span class="spinner"></span>Running tests and building report for ${escapeHtml(ticketId)}…</div>`;
  log(`Generating test report for ${ticketId}…`, "info");
  try {
    const apiBase = $("apiBaseUrl").value.trim();
    const payload = runPrControlValues(ticketId);
    if (apiBase) payload.api_base_url = apiBase;
    const data = await api(`/api/dashboard/outputs/${encodeURIComponent(ticketId)}/report`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
    renderTestResults(data, { generated_at: data.generated_at, report_files: data.report_files });
    const failed = data.failed + data.errors;
    log(
      `Report for ${ticketId}: ${data.passed} passed, ${failed} failed — ready to download`,
      failed === 0 && data.total > 0 ? "ok" : "err"
    );
    if (data.pr_url) log(`PR ready: ${data.pr_url}`, "ok");
    else if (data.pr_message) log(data.pr_message, "err");
    await loadOutputs();
  } catch (e) {
    if (!isStaleEpoch(epoch) && ticketId === window.__activeTicketId) {
      $("resultTests").innerHTML = `<div class="empty-state" style="color:var(--error)">${escapeHtml(e.message)}</div>`;
      log(`Report generation failed for ${ticketId}: ${e.message}`, "err");
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

async function runTests(ticketId, btn) {
  const original = btn ? btn.textContent : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Running…";
  }
  const epoch = window.__resultsEpoch;
  setActiveReportTicket(ticketId);
  $("resultTests").innerHTML = `<div class="empty-state"><span class="spinner"></span>Running ${escapeHtml(ticketId)} tests…</div>`;
  log(`Running tests for ${ticketId}…`, "info");
  try {
    const apiBase = $("apiBaseUrl").value.trim();
    const payload = runPrControlValues(ticketId);
    if (apiBase) payload.api_base_url = apiBase;
    const data = await api(`/api/dashboard/outputs/${ticketId}/run`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
    renderTestResults(data);
    const failed = data.failed + data.errors;
    log(
      `Tests for ${ticketId}: ${data.passed} passed, ${failed} failed (${data.total} total)`,
      failed === 0 && data.total > 0 ? "ok" : "err"
    );
    if (data.pr_url) log(`PR ready: ${data.pr_url}`, "ok");
    else if (data.pr_message) log(data.pr_message, "err");
  } catch (e) {
    if (!isStaleEpoch(epoch) && ticketId === window.__activeTicketId) {
      $("resultTests").innerHTML = `<div class="empty-state" style="color:var(--error)">${escapeHtml(e.message)}</div>`;
      log(`Test run failed for ${ticketId}: ${e.message}`, "err");
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

async function runPhase1() {
  const ticketId = selectTicket($("ticketId").value.trim(), { silent: true });
  const epoch = window.__resultsEpoch;
  resetSteps();
  setStepState(1, "active");
  const payload = buildPayload();
  log(`Phase 1: processing ${payload.ticket_id}…`, "info");
  const resp = await api("/process-ticket", { method: "POST", body: JSON.stringify(payload) });
  if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
  renderPhase1(resp.data);
  log(`Phase 1 complete — ${(resp.data.acceptance_criteria || []).length} criteria`, "ok");
}

async function runPhase2() {
  const ticketId = selectTicket($("ticketId").value.trim(), { silent: true });
  const epoch = window.__resultsEpoch;
  resetSteps();
  setStepState(1, "active");
  const payload = buildPayload();
  log(`Phase 1: processing ${payload.ticket_id}…`, "info");
  const p1 = await api("/process-ticket", { method: "POST", body: JSON.stringify(payload) });
  if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
  renderPhase1(p1.data);
  setStepState(2, "active");
  log(`Phase 2: generating scenarios…`, "info");
  const resp = await api("/generate-scenarios", { method: "POST", body: JSON.stringify(payload) });
  if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
  renderPhase2(resp.data);
  log(`Phase 2 complete — ${resp.data.scenarios.length} scenarios`, "ok");
  await loadOutputs();
}

async function runFull() {
  const ticketId = selectTicket($("ticketId").value.trim(), { silent: true });
  const epoch = window.__resultsEpoch;
  resetSteps();
  setStepState(1, "active");
  const payload = buildPayload();
  log(`Phase 1: processing ${payload.ticket_id}…`, "info");
  const p1 = await api("/process-ticket", { method: "POST", body: JSON.stringify(payload) });
  if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
  renderPhase1(p1.data);
  setStepState(2, "active");
  setStepState(3, "active");
  log(`Phases 2+3: generating scenarios and Pytest scripts… (auto-retries on rate limit, may take 2–4 min)`, "info");
  const resp = await api("/generate-scripts", { method: "POST", body: JSON.stringify(payload) });
  if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
  renderPhase2(resp.scenarios);
  renderPhase3(resp.data);
  log(`Pipeline complete — ${resp.data.files.length} files generated`, "ok");
  await loadOutputs();

  // When auto-Create-PR is on: run tests, then open the PR regardless of outcome
  if ($("createPr").checked) {
    log(`Create-PR toggle on — running generated suite for ${payload.ticket_id}…`, "info");
    $("resultTests").innerHTML = `<div class="empty-state"><span class="spinner"></span>Running ${escapeHtml(payload.ticket_id)} tests before PR…</div>`;
    const apiBase = $("apiBaseUrl").value.trim();
    const testPayload = {
      ...runPrControlValues(payload.ticket_id),
      create_pr: true,
    };
    if (apiBase) testPayload.api_base_url = apiBase;
    const testResp = await api(`/api/dashboard/outputs/${encodeURIComponent(payload.ticket_id)}/run`, {
      method: "POST",
      body: JSON.stringify(testPayload),
    });
    if (isStaleEpoch(epoch) || ticketId !== window.__activeTicketId) return;
    renderTestResults(testResp);
    const failed = testResp.failed + testResp.errors;
    log(
      `Tests for ${payload.ticket_id}: ${testResp.passed} passed, ${failed} failed (${testResp.total} total)`,
      failed === 0 && testResp.total > 0 ? "ok" : "err"
    );
    if (testResp.pr_url) log(`PR ready: ${testResp.pr_url}`, "ok");
    else if (testResp.pr_message) log(testResp.pr_message, "err");
  }
}

$("runPhase1").onclick = async () => {
  setLoading(true);
  try {
    await runPhase1();
  } catch (e) {
    log(e.message, "err");
  } finally {
    setLoading(false);
  }
};

$("runPhase2").onclick = async () => {
  setLoading(true);
  try {
    await runPhase2();
  } catch (e) {
    log(e.message, "err");
  } finally {
    setLoading(false);
  }
};

$("runFull").onclick = async () => {
  setLoading(true);
  try {
    await runFull();
  } catch (e) {
    log(e.message, "err");
  } finally {
    setLoading(false);
  }
};

$("refreshTickets").onclick = () => loadTickets();
$("refreshOutputs").onclick = loadOutputs;
$("apiType").onchange = updateSpecUrlLabel;

$("generateReportBtn").onclick = () => {
  const ticketId = window.__activeReportTicket || $("ticketId").value.trim();
  if (!ticketId) {
    log("Select a ticket with generated tests first", "err");
    return;
  }
  generateReport(ticketId, $("generateReportBtn"));
};

$("downloadReportBtn").onclick = () => {
  const ticketId = window.__activeReportTicket || $("ticketId").value.trim();
  if (!ticketId) {
    log("No report ticket selected", "err");
    return;
  }
  downloadReport(ticketId);
};

$("raisePrBtn").onclick = () => {
  const ticketId = window.__activeReportTicket || $("ticketId").value.trim();
  if (!ticketId) {
    log("Select a ticket with generated tests first", "err");
    return;
  }
  raisePr(ticketId, $("raisePrBtn"));
};

$("prOptionsToggle").onclick = () => {
  const box = $("prOptions");
  box.hidden = !box.hidden;
  $("prOptionsToggle").setAttribute("aria-expanded", String(!box.hidden));
  if (!box.hidden) updatePrPlan();
};

$("inspectRepoBtn").onclick = () => inspectTargetRepo($("inspectRepoBtn"));

// Every control updates the "this is what will happen" line beneath them.
["prGrouping", "prScopeSelect", "prAdaptTests", "prAnalyzeRepo"].forEach((id) => {
  $(id).onchange = updatePrPlan;
});
["prBranch", "prTargetPath", "prTitle"].forEach((id) => {
  $(id).addEventListener("input", updatePrPlan);
});

// Adaptation is meaningless without the analysis it adapts to.
$("prAdaptTests").addEventListener("change", () => {
  if ($("prAdaptTests").checked && !$("prAnalyzeRepo").checked) {
    $("prAnalyzeRepo").checked = true;
    log("Enabled “Read the target repo first” — adaptation needs it", "info");
  }
  updatePrPlan();
});
$("prAnalyzeRepo").addEventListener("change", () => {
  if (!$("prAnalyzeRepo").checked && $("prAdaptTests").checked) {
    $("prAdaptTests").checked = false;
    log("Disabled “Adapt tests” — it needs the target repo to be read first", "info");
  }
  updatePrPlan();
});

function onTicketIdEdited() {
  const ticketId = selectTicket($("ticketId").value.trim());
  if (ticketId) checkReportStatus(ticketId);
}

$("ticketId").addEventListener("change", onTicketIdEdited);
$("ticketId").addEventListener("keydown", (e) => {
  if (e.key === "Enter") onTicketIdEdited();
});

(async function init() {
  updateSpecUrlLabel();
  try {
    await loadConfig();
    await loadHealth();
    await loadTickets();
    await loadOutputs();
    log("Dashboard ready", "ok");
  } catch (e) {
    log(`Init failed: ${e.message}`, "err");
  }
})();
