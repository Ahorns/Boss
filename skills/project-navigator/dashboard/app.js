// Polls /api/state once a second and re-renders only when the payload's rev
// changes. The server recomputes from roadmap.yaml on every request, so a `pnav
// done ...` in another terminal shows up here within ~1s with no refresh.

const POLL_MS = 1000;
const PROJECT_Q = new URLSearchParams(location.search).get("p");
const STATE_URL = "/api/state" + (PROJECT_Q ? `?p=${encodeURIComponent(PROJECT_Q)}` : "");

let lastRev = null;
let selected = null;   // node id the user clicked, or null to follow current
let currentId = null;
let sectionMode = "now";  // "now" | "history" | "retired"
let nowView = "tree";      // "tree" | "map"
let orientation = "td";    // top-down is the readable default; LR remains available
let mapBounds = null;

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function pct(frac) {
  return frac === null || frac === undefined ? "—" : `${(frac * 100).toFixed(1)}%`;
}

// Notes are authored in YAML literal blocks, so they arrive with hard line
// breaks at ~78 chars. Rejoin those, but keep blank lines as paragraph breaks.
function reflow(s) {
  return String(s)
    .split(/\n\s*\n/)
    .map((p) => p.replace(/\s*\n\s*/g, " ").trim())
    .filter(Boolean)
    .join("\n\n");
}

function block(label, bodyHtml, cls = "") {
  if (!bodyHtml) return "";
  return `<div class="d-block">
            <div class="d-label">${esc(label)}</div>
            <div class="d-body ${cls}">${bodyHtml}</div>
          </div>`;
}

// --------------------------------------------------------------------- tree

// Collapsed ids are stored, not expanded ones, so a node added to the roadmap
// tomorrow shows up open rather than silently hidden.
let collapsed = (() => {
  try {
    const raw = JSON.parse(localStorage.getItem("pnav-collapsed") || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch { return new Set(); }
})();

function saveCollapsed() {
  try { localStorage.setItem("pnav-collapsed", JSON.stringify([...collapsed])); } catch { /* */ }
}

function childrenOf(state) {
  const kids = new Map();
  for (const n of state.nodes) {
    if (!n.parent) continue;
    if (!kids.has(n.parent)) kids.set(n.parent, []);
    kids.get(n.parent).push(n);
  }
  return kids;
}

function rowHtml(n, kids) {
  const children = kids.get(n.id) || [];
  const open = !collapsed.has(n.id);
  const knob = children.length
    ? `<button class="knob ${open ? "open" : ""}" data-toggle="${esc(n.id)}"
         type="button" aria-label="${open ? "Collapse" : "Expand"} ${esc(n.name)}"
         aria-expanded="${open}">${open ? "\u2212" : "+"}</button>`
    : `<span class="knob-spacer"></span>`;

  const frac = n.progress;
  const cls = [
    "row",
    `status-${n.status}`,
    n.is_current ? "current" : "",
    n.id === selected ? "selected" : "",
  ].filter(Boolean).join(" ");

  return `<div class="${cls}" data-id="${esc(n.id)}" role="treeitem" tabindex="0"
    aria-selected="${n.id === selected}"${children.length ? ` aria-expanded="${open}"` : ""}>
    ${knob}
    <span class="dot dot-${esc(n.status)}" title="${esc(n.status)}"></span>
    <span class="nid">${esc(n.id)}</span>
    <span class="nname">${esc(n.name)}</span>
    ${n.is_current ? `<span class="here">you are here</span>` : ""}
    <span class="nmeta">${esc(n.kind || "task")} · ${esc((n.outcome || "active").replace("_", " "))}</span>
    <span class="minibar"><i style="width:${frac === null ? 0 : frac * 100}%"></i></span>
    <span class="nprog">${frac === null ? "\u2014" : pct(frac)}</span>
  </div>`;
}

function treeItemHtml(n, kids, root = false) {
  const children = kids.get(n.id) || [];
  const body = !children.length || collapsed.has(n.id) ? "" :
    `<div class="tree-children" role="group">${children.map((c) =>
      treeItemHtml(c, kids)).join("")}</div>`;
  return `<div class="tree-item${root ? " tree-root" : ""}">${rowHtml(n, kids)}${body}</div>`;
}

// The tree is one continuous hierarchy. Top-level separators make a forest
// legible without turning every branch into an isolated card.
function renderTree(state) {
  const kids = childrenOf(state);
  const roots = state.nodes.filter((n) => !n.parent);
  const tree = $("tree");

  tree.setAttribute("role", "tree");
  tree.innerHTML = roots.length
    ? roots.map((n) => treeItemHtml(n, kids, true)).join("")
    : `<div class="tree-empty">No roadmap nodes yet.</div>`;

  tree.querySelectorAll("[data-toggle]").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();          // toggling is not selecting
      const id = b.dataset.toggle;
      if (collapsed.has(id)) collapsed.delete(id);
      else collapsed.add(id);
      saveCollapsed();
      renderAll(window.__state);
    });
  });

  tree.querySelectorAll(".row").forEach((r) => {
    const choose = () => {
      selected = r.dataset.id === selected ? null : r.dataset.id;
      renderAll(window.__state);
    };
    r.addEventListener("click", choose);
    r.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        choose();
      }
    });
  });
}

// ------------------------------------------------------------------- detail

function renderDetail(state) {
  const showId = selected || currentId;
  const n = state.nodes.find((x) => x.id === showId);
  const pane = $("detail");

  $("back").hidden = !(selected && selected !== currentId);
  $("detail-title").textContent =
    !n ? "Detail" : (n.id === currentId ? modeLabel(state.mode) : "Inspecting");

  if (!n) {
    pane.innerHTML = `<div class="faint">${
      state.mode === "complete" ? "Every node is resolved — the project is complete."
                                : "Nothing selected."}</div>`;
    return;
  }

  const criteria = (n.criteria || []).length
    ? `<ul>${n.criteria.map((c) =>
        `<li class="${c.met ? "met" : ""}"><span class="mark">${c.met ? "✓" : "·"}</span>
         <span>${esc(c.text)}</span></li>`).join("")}</ul>`
    : "";

  const evidence = (n.evidence || []).length
    ? `<ul>${n.evidence.map((e) =>
        `<li><span class="mark">→</span><span>${esc(e)}</span></li>`).join("")}</ul>`
    : "";

  const blockers = (n.unmet_blockers || []).length
    ? esc(n.unmet_blockers.join(", "))
    : "";

  const detail = [
    block("Research question", n.question ? esc(reflow(n.question)) : ""),
    block("Experiment", n.experiment ? esc(reflow(n.experiment)) : ""),
    block("Goal", n.goal ? esc(reflow(n.goal)) : ""),
    block("Success criteria", criteria),
    block("Evidence", evidence, "mono"),
    block("Next action", n.next_action ? esc(n.next_action) : "", "d-next"),
    block("Blocked by", blockers, "d-blocked"),
    block("Note", n.note ? `<div class="d-note">${esc(reflow(n.note))}</div>` : ""),
  ].filter(Boolean).join("");

  pane.innerHTML = `
    <div class="d-summary">
      <div class="d-head"><span class="d-id">${esc(n.id)}</span></div>
      <div class="d-name">${esc(n.name)}</div>
      <span class="pill p-kind">${esc(n.kind || "task")}</span>
      <span class="pill p-${esc(n.status)}">${esc(n.status.replace("_", " "))}</span>
      <span class="pill p-outcome outcome-${esc(n.outcome || "active")}">${esc((n.outcome || "active").replace("_", " "))}</span>
      ${n.weight !== 1 ? `<span class="pill p-todo">weight ${n.weight}</span>` : ""}
      <div class="d-bar">
        <div class="bar"><div class="bar-fill" style="width:${
          n.progress === null ? 0 : n.progress * 100}%"></div></div>
        <div class="head-meta"><span class="pct">${pct(n.progress)}</span></div>
      </div>
    </div>
    ${(state.current_branches || []).length && n.id === currentId ? `
      <div class="branch-summary">${state.current_branches.map((b) => `
        <div class="branch branch-${esc(b.when)}"><span>${esc(b.when.toUpperCase())}</span>
          <strong>${esc(b.to)}</strong> ${esc(b.name || "")}</div>`).join("")}</div>` : ""}
    ${detail ? `<div class="d-content">${detail}</div>` : ""}
  `;
}

// The roadmap says where you are; the log says how you got here. Newest first,
// because the question is almost always "what just happened".
const EVENT_TONE = {
  DONE: "ev-done", FAILED: "ev-failed", IN_PROGRESS: "ev-wip",
  BLOCKED: "ev-blocked", DEFERRED: "ev-deferred", PLAN_CHANGE: "ev-plan",
  INIT: "ev-init",
};

function renderActivity(state) {
  const evs = (state.events || []).slice().reverse();
  const host = $("activity");
  if (!evs.length) {
    host.innerHTML = `<div class="empty-state"><strong>No activity yet.</strong><span>Execution and outcome events will appear here.</span></div>`;
    return;
  }

  let lastDay = null;
  const rows = evs.map((e) => {
    const t = String(e.time || "");
    const day = t.slice(0, 10);
    const clock = t.slice(11, 16);
    const head = day && day !== lastDay ? `<div class="ev-day">${esc(day)}</div>` : "";
    lastDay = day || lastDay;
    const tone = EVENT_TONE[e.event] || "ev-plain";
    const node = e.node ? `<span class="ev-node">${esc(e.node)}</span>` : "";
    const name = e.name ? `<span class="ev-name">${esc(e.name)}</span>` : "";
    const msg = e.message ? `<div class="ev-msg">${esc(e.message)}</div>` : "";
    const evidence = (e.evidence || []).length
      ? `<div class="ev-extra">${e.evidence.map(esc).join("  ")}</div>` : "";
    const changes = (e.changes || []).length
      ? `<ul class="ev-changes">${e.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : "";
    const from = e.from !== undefined && e.to !== undefined
      ? `<span class="ev-from">${esc(e.from)} → ${esc(e.to)}</span>` : "";
    return `${head}<div class="ev ${tone}">
      <div class="ev-time">${esc(clock)}</div><div class="ev-dot"></div>
      <div class="ev-body"><div class="ev-top">
        <span class="ev-kind">${esc(String(e.event || "?").replaceAll("_", " "))}</span>
        ${node}${name}${from}</div>${msg}${evidence}${changes}</div></div>`;
  });
  host.innerHTML = `<div class="timeline">${rows.join("")}</div>`;
}

function renderHistory(state) {
  const host = $("history");
  const proposals = (state.proposals || []).slice().reverse();
  const changes = (state.changes || []).slice().reverse();
  const versions = (state.history || []).slice().reverse();

  const proposalHtml = proposals.length ? proposals.map((p) => `
    <article class="record proposal-${esc(p.status)}">
      <div class="record-top"><span class="record-id">${esc(p.proposal_id)}</span>
        <span class="record-state">${esc(p.status)}</span></div>
      <strong>${esc(p.reason)}</strong>
      ${(p.suggested_changes || []).length ? `<ul>${p.suggested_changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      ${p.resolution ? `<p>${esc(p.resolution)}</p>` : ""}
    </article>`).join("") : `<div class="faint">No plan proposals.</div>`;

  const changeHtml = changes.length ? changes.map((c) => `
    <article class="record">
      <div class="record-top"><span class="record-id">${esc(c.change_id || "change")}</span>
        <span class="record-state">v${esc(c.from_version)} → v${esc(c.to_version)}</span></div>
      <strong>${esc(c.reason)}</strong>
      ${(c.changes || []).length ? `<ul>${c.changes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    </article>`).join("") : `<div class="faint">No accepted plan changes.</div>`;

  const versionHtml = versions.length ? versions.map((v) => `
    <div class="version-row"><strong>v${esc(v.version)}</strong><span>${esc(v.node_count)} nodes</span>
      <span>${v.current ? `current ${esc(v.current)}` : "no explicit current"}</span></div>`).join("")
    : `<div class="faint">No version snapshots yet.</div>`;

  host.innerHTML = `<div class="history-grid">
    <section><h2>Plan versions</h2>${versionHtml}</section>
    <section><h2>Proposals</h2>${proposalHtml}</section>
    <section><h2>Accepted changes</h2>${changeHtml}</section>
  </div>`;
}

function renderRetired(state) {
  const host = $("retired");
  const rows = state.retired || [];
  host.innerHTML = rows.length ? `<div class="retired-list">${rows.map((n) => `
    <article class="retired-row">
      <span class="d-id">${esc(n.id)}</span><strong>${esc(n.name || "Unnamed path")}</strong>
      <span class="pill p-outcome outcome-${esc(n.outcome)}">${esc(String(n.outcome).replace("_", " "))}</span>
      <span class="record-state">last in v${esc(n.version)}</span>
      ${n.note ? `<p>${esc(reflow(n.note))}</p>` : ""}
    </article>`).join("")}</div>` :
    `<div class="empty-state"><strong>No retired paths.</strong><span>Superseded, abandoned, deferred, and removed paths will remain available here.</span></div>`;
}

function modeLabel(mode) {
  return { in_progress: "Current", explicit: "Current", next_up: "Next up",
           awaiting_outcome: "Awaiting outcome", blocked: "Stalled",
           complete: "Complete" }[mode] || "Current";
}

// -------------------------------------------------------------------- shell

function renderAll(state) {
  window.__state = state;
  currentId = state.current;

  $("project").textContent = state.project ?? "—";
  $("root").textContent = state.root ?? "";
  $("bar-fill").style.width = `${(state.progress ?? 0) * 100}%`;
  $("pct").textContent = pct(state.progress);
  $("plan-version").textContent = `v${state.plan_version || 1}`;

  const counts = state.leaf_counts || {};
  $("counts").textContent = Object.entries(counts)
    .map(([k, v]) => `${v} ${k.replace("_", " ")}`).join("   ");

  const current = state.current_node || state.nodes.find((n) => n.id === currentId);
  $("focus").dataset.mode = state.mode || "";
  $("focus-mode").textContent = modeLabel(state.mode);
  $("focus-id").textContent = current ? current.id : "";
  $("focus-name").textContent = current
    ? current.name
    : (state.mode === "complete" ? "Project complete" : "No current work selected");
  $("focus-next").textContent = current && current.next_action
    ? current.next_action
    : (state.mode === "complete" ? "No further action recorded." : "No next action recorded.");
  $("focus-open").hidden = !current;

  document.title = `${pct(state.progress)} · ${state.project ?? "Project Navigator"}`;

  if (sectionMode === "history") renderHistory(state);
  else if (sectionMode === "activity") renderActivity(state);
  else if (sectionMode === "retired") renderRetired(state);
  else if (nowView === "map") renderMap(state);
  else renderTree(state);

  const pc = state.plan_change || [];
  $("planchange").hidden = pc.length === 0;
  if (pc.length) {
    $("planchange").innerHTML =
      `<strong>\u26a0 PLAN CHANGED</strong> \u2014 the shape of the roadmap was edited outside
       the CLI and has not been explained.
       <ul>${pc.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
       <code>pnav plan-change -m "&lt;why&gt;"</code>`;
  }
  renderDetail(state);
}

function renderMap(state) {
  const keepCamera = mapBounds !== null;
  const saved = { ...MAP.view };
  mapBounds = MAP.render(state, {
    orientation,
    selected: selected || currentId,
    onSelect: (id) => { selected = id === selected ? null : id; renderAll(window.__state); },
  });
  if (keepCamera) {
    // Keep the viewport where the user left it across a live update.
    Object.assign(MAP.view, saved);
    MAP.applyCamera();
  } else {
    MAP.fit(mapBounds);
  }
}

function applyWorkspaceVisibility() {
  $("tree").hidden = sectionMode !== "now" || nowView !== "tree";
  $("map-wrap").hidden = sectionMode !== "now" || nowView !== "map";
  $("history").hidden = sectionMode !== "history";
  $("activity").hidden = sectionMode !== "activity";
  $("retired").hidden = sectionMode !== "retired";
  $("nowctl").hidden = sectionMode !== "now";
  $("focus").hidden = sectionMode !== "now";
  $("detail-pane").hidden = sectionMode !== "now";
  document.querySelector(".workspace-shell").classList.toggle("single-pane", sectionMode !== "now");
  for (const mode of ["now", "history", "activity", "retired"])
    $(`tab-${mode}`).classList.toggle("on", sectionMode === mode);
  $("view-tree").classList.toggle("on", nowView === "tree");
  $("view-map").classList.toggle("on", nowView === "map");
}

function setSection(mode) {
  sectionMode = mode;
  applyWorkspaceVisibility();
  try { localStorage.setItem("pnav-section", mode); } catch { /* private mode */ }
  if (window.__state) renderAll(window.__state);
}

function setNowView(mode) {
  nowView = mode;
  if (mode === "map") mapBounds = null;
  applyWorkspaceVisibility();
  try { localStorage.setItem("pnav-now-view", mode); } catch { /* private mode */ }
  if (window.__state) renderAll(window.__state);
}

function setOrientation(o) {
  orientation = o;
  $("orient").textContent = o === "lr" ? "Left to right" : "Top to bottom";
  try { localStorage.setItem("pnav-orient-v2", o); } catch { /* private mode */ }
  mapBounds = null;
  if (window.__state) renderAll(window.__state);
}

function flashLive() {
  const el = $("live");
  el.classList.remove("pulse");
  void el.offsetWidth;          // restart the animation
  el.classList.add("pulse");
}

async function poll() {
  try {
    const res = await fetch(STATE_URL, { cache: "no-store" });
    const state = await res.json();

    if (state.error) {
      $("error").hidden = false;
      $("error").textContent = state.error;
      return;
    }
    $("error").hidden = true;

    const warns = state.warnings || [];
    $("warn").hidden = warns.length === 0;
    $("warn").textContent = warns.map((w) => `\u26a0 ${w}`).join("\n");

    if (state.rev === lastRev) return;   // nothing changed; leave the DOM alone
    const first = lastRev === null;

    const scroll = $("tree").scrollTop;
    try {
      renderAll(state);
    } catch (err) {
      // Do not advance lastRev: a render bug must not silently stick, and the
      // banner must not be cleared by the next poll as if all were well.
      $("error").hidden = false;
      $("error").textContent = `render failed — the page is showing stale state.\n${err && err.stack || err}`;
      return;
    }
    lastRev = state.rev;
    $("tree").scrollTop = scroll;
    if (!first) flashLive();
  } catch (err) {
    $("error").hidden = false;
    $("error").textContent = `dashboard cannot reach the server — is \`pnav serve\` still running?\n${err}`;
  }
}

// Theme: URL param wins for one-off checks, otherwise the stored choice, and
// with neither set the page just follows the OS.
const THEMES = ["system", "light", "dark"];

function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("theme").textContent = t;
}

function initTheme() {
  const fromUrl = new URLSearchParams(location.search).get("theme");
  let t = fromUrl;
  if (!THEMES.includes(t)) {
    try { t = localStorage.getItem("pnav-theme"); } catch { t = null; }
  }
  applyTheme(THEMES.includes(t) ? t : "system");
}

$("theme").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf($("theme").textContent) + 1) % THEMES.length];
  applyTheme(next);
  try { localStorage.setItem("pnav-theme", next); } catch { /* private mode */ }
});

initTheme();

$("tab-now").addEventListener("click", () => setSection("now"));
$("tab-history").addEventListener("click", () => setSection("history"));
$("tab-activity").addEventListener("click", () => setSection("activity"));
$("tab-retired").addEventListener("click", () => setSection("retired"));
$("view-tree").addEventListener("click", () => setNowView("tree"));
$("view-map").addEventListener("click", () => setNowView("map"));
$("orient").addEventListener("click", () => setOrientation(orientation === "lr" ? "td" : "lr"));
$("fit").addEventListener("click", () => {
  if (mapBounds) MAP.fit(mapBounds);
});

(function initView() {
  // URL params win, so a view can be linked or bookmarked; otherwise the
  // remembered choice; otherwise the default.
  const q = new URLSearchParams(location.search);
  let section = q.get("section"), v = q.get("view"), o = q.get("orient");
  try {
    if (!section) section = localStorage.getItem("pnav-section");
    if (!v) v = localStorage.getItem("pnav-now-view") || localStorage.getItem("pnav-view");
    if (!o) o = localStorage.getItem("pnav-orient-v2");
  } catch { /* private mode */ }
  orientation = o === "lr" ? "lr" : "td";
  $("orient").textContent = orientation === "lr" ? "Left to right" : "Top to bottom";
  nowView = v === "map" ? "map" : "tree";
  setSection(["history", "activity", "retired"].includes(section) ? section : "now");
})();

$("back").addEventListener("click", () => {
  selected = null;
  renderAll(window.__state);
});

$("focus-open").addEventListener("click", () => {
  selected = null;
  renderAll(window.__state);
  $("detail-pane").scrollIntoView({ behavior: "smooth", block: "start" });
});

// Only offer the way back when the server actually has more than one project.
fetch("/api/hub", { cache: "no-store" })
  .then((r) => r.json())
  .then((d) => { $("hublink").hidden = !d.multi; })
  .catch(() => { /* single-project server, or not reachable yet */ });

poll();
setInterval(poll, POLL_MS);
