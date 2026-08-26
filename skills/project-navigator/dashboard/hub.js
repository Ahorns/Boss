// Overview across every project the server was pointed at. One compact row per
// project: how far along, what it is waiting on, and what to do next.

const POLL_MS = 2000;   // slower than the single-project view; this is a glance
let lastKey = null;

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const pct = (f) => (f === null || f === undefined ? "—" : `${(f * 100).toFixed(0)}%`);

const MODE_LABEL = {
  in_progress: "current", explicit: "current", next_up: "next up",
  awaiting_outcome: "awaiting outcome", blocked: "stalled", complete: "complete",
};

function card(p) {
  if (p.error) {
    return `<a class="card card-error" href="/?p=${encodeURIComponent(p.root)}">
      <div class="card-main">
        <div class="card-name">${esc(p.project || p.root)}</div>
        <div class="card-path">${esc(p.root)}</div>
        <div class="card-err">${esc(p.error.split("\n")[0])}</div>
      </div>
      <span class="card-open" aria-hidden="true">→</span>
    </a>`;
  }

  const counts = Object.entries(p.leaf_counts || {})
    .map(([k, v]) => `<span class="cc cc-${esc(k)}">${v} ${esc(k.replace("_", " "))}</span>`)
    .join("");

  const flags = [
    p.proposals ? `<span class="flag">${p.proposals} proposal${p.proposals > 1 ? "s" : ""}</span>` : "",
    p.plan_change ? `<span class="flag flag-plan">plan changed</span>` : "",
    p.warnings ? `<span class="flag">${p.warnings} warning${p.warnings > 1 ? "s" : ""}</span>` : "",
  ].join("");

  return `<a class="card mode-${esc(p.mode)}" href="/?p=${encodeURIComponent(p.root)}">
    <div class="card-main">
      <div class="card-top"><div class="card-name">${esc(p.project)}</div><span class="plan-version">v${esc(p.plan_version || 1)}</span></div>
      <div class="card-path">${esc(p.root)}</div>
    </div>
    <div class="card-work">
      <div class="card-cur">
        <span class="card-mode">${esc(MODE_LABEL[p.mode] || "current")}</span>
        ${p.current ? `<span class="card-id">${esc(p.current)}</span>` : ""}
        <span class="card-cname">${esc(p.current_name || "—")}</span>
      </div>
      ${p.next_action ? `<div class="card-next"><strong>Next:</strong> ${esc(p.next_action)}</div>` : ""}
    </div>
    <div class="card-progress">
      <div class="card-pct">${pct(p.progress)}</div>
      <div class="bar"><div class="bar-fill" style="width:${(p.progress || 0) * 100}%"></div></div>
      <div class="card-foot">${counts}${flags}</div>
    </div>
    <span class="card-open" aria-hidden="true">→</span>
  </a>`;
}

function render(data) {
  const ps = data.projects || [];
  $("cards").innerHTML = ps.map(card).join("") ||
    `<div class="faint">No projects found.</div>`;

  const live = ps.filter((p) => !p.error);
  const mean = live.length
    ? live.reduce((a, p) => a + (p.progress || 0), 0) / live.length : 0;
  $("bar-fill").style.width = `${mean * 100}%`;
  $("pct").textContent = pct(mean);
  $("counts").textContent =
    `${live.length} project${live.length === 1 ? "" : "s"}` +
    (ps.length - live.length ? `   ${ps.length - live.length} broken` : "");
  document.title = `${pct(mean)} · Projects · Project Navigator`;
}

async function poll() {
  try {
    const data = await (await fetch("/api/hub", { cache: "no-store" })).json();
    $("error").hidden = true;
    const key = JSON.stringify((data.projects || []).map((p) => [p.root, p.rev, p.error]));
    if (key === lastKey) return;
    try {
      render(data);
    } catch (err) {
      $("error").hidden = false;
      $("error").textContent = `render failed\n${(err && err.stack) || err}`;
      return;
    }
    lastKey = key;
  } catch (err) {
    $("error").hidden = false;
    $("error").textContent = `cannot reach the server — is \`pnav hub\` still running?\n${err}`;
  }
}

// Theme handling mirrors the project view so the two pages agree.
const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("theme").textContent = t;
}
(function initTheme() {
  let t = new URLSearchParams(location.search).get("theme");
  if (!THEMES.includes(t)) {
    try { t = localStorage.getItem("pnav-theme"); } catch { t = null; }
  }
  applyTheme(THEMES.includes(t) ? t : "system");
})();
$("theme").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf($("theme").textContent) + 1) % THEMES.length];
  applyTheme(next);
  try { localStorage.setItem("pnav-theme", next); } catch { /* private mode */ }
});

poll();
setInterval(poll, POLL_MS);
