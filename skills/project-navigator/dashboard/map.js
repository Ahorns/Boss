// Hierarchical roadmap graph. It deliberately mirrors the tree view: bordered
// rectangular nodes, a narrow status rail, the same labels, and the same
// current/selected states. A generic project root connects every top-level
// phase, so a roadmap is one coherent graph rather than a set of floating trees.

const MAP = (() => {
  const NODE_W_LR = 230;
  const NODE_W_TD = 190;
  const LEVEL_GAP_LR = 72;
  const LEVEL_GAP_TD = 58;
  const SIBLING_GAP_LR = 20;
  const SIBLING_GAP_TD = 28;
  // Separate independent trees without making a roadmap with many top-level
  // phases shrink to illegibility when fitted into the viewport.
  const ROOT_GAP = 16;

  const view = { x: 0, y: 0, k: 1 };
  let controlsBound = false;
  let onSelectRef = null;
  let nameMeasurer = null;

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function statusLabel(status) {
    return String(status || "todo").replace("_", " ");
  }

  function boxHeight(name, width) {
    // Character counts are not a reliable proxy for wrapping: proportional
    // fonts, long English words, and mixed CJK text all break differently.
    // Measure with the exact map-title CSS so the SVG box follows the browser's
    // real line layout and never clips a project-agnostic node name.
    if (typeof document !== "undefined" && document.body) {
      if (!nameMeasurer) {
        nameMeasurer = document.createElement("div");
        nameMeasurer.className = "map-name-measure";
        nameMeasurer.setAttribute("aria-hidden", "true");
        document.body.appendChild(nameMeasurer);
      }
      // foreignObject starts after the 4px status rail; the body adds another
      // 34px horizontal padding. Keep 2px safety for sub-pixel font metrics.
      nameMeasurer.style.width = `${Math.max(40, width - 40)}px`;
      nameMeasurer.textContent = String(name || "");
      return 62 + Math.max(22, Math.ceil(nameMeasurer.getBoundingClientRect().height));
    }

    // Non-DOM fallback for callers that only inspect the algorithm.
    const charsPerLine = Math.max(12, Math.floor((width - 34) / 8));
    const lines = Math.max(1, Math.ceil(String(name || "").length / charsPerLine));
    return 62 + lines * 22;
  }

  function computeLayout(state, orientation = "lr") {
    const horizontal = orientation === "lr";
    const width = horizontal ? NODE_W_LR : NODE_W_TD;
    const siblingGap = horizontal ? SIBLING_GAP_LR : SIBLING_GAP_TD;
    const levelGap = horizontal ? LEVEL_GAP_LR : LEVEL_GAP_TD;
    const byId = new Map();
    const order = new Map();
    (state.nodes || []).forEach((source, i) => {
      byId.set(source.id, { ...source, w: width, h: boxHeight(source.name, width) });
      order.set(source.id, i);
    });

    // State edges contain both legacy containment and explicit research flow.
    // Deduplicate exact relationships so mixed old/new plans remain readable.
    const seen = new Set();
    const edges = [];
    for (const source of state.edges || []) {
      if (!byId.has(source.from) || !byId.has(source.to)) continue;
      const when = source.when || "next";
      const key = `${source.from}\u0000${source.to}\u0000${when}`;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ from: source.from, to: source.to, when, label: source.label });
    }

    // Older state payloads did not expose edges. Preserve their hierarchy.
    if (!edges.length) {
      for (const source of state.nodes || []) {
        if (source.parent && byId.has(source.parent))
          edges.push({ from: source.parent, to: source.id, when: "contains" });
      }
    }

    const incoming = new Map([...byId.keys()].map((id) => [id, []]));
    const outgoing = new Map([...byId.keys()].map((id) => [id, []]));
    for (const edge of edges) {
      incoming.get(edge.to).push(edge);
      outgoing.get(edge.from).push(edge);
    }
    const roots = [...byId.keys()].filter((id) => !incoming.get(id).length)
      .sort((a, b) => order.get(a) - order.get(b));
    const project = {
      id: "__project__",
      name: state.project || "Project",
      status: state.mode === "complete" ? "done" : "in_progress",
      progress: state.progress,
      is_current: false,
      isProject: true,
      w: width,
      h: boxHeight(state.project || "Project", width),
    };
    const all = byId.size ? [project, ...byId.values()] : [];
    const graphEdges = roots.map((id) => ({
      parent: project, child: byId.get(id), when: "root", synthetic: true,
    }));
    for (const edge of edges) graphEdges.push({
      parent: byId.get(edge.from), child: byId.get(edge.to),
      when: edge.when, label: edge.label,
    });

    // Stable Kahn order plus longest-path ranks gives a lightweight DAG layout
    // that supports branches and merges without a graph dependency.
    const indegree = new Map([...incoming].map(([id, rows]) => [id, rows.length]));
    const queue = roots.slice();
    const topo = [];
    while (queue.length) {
      queue.sort((a, b) => order.get(a) - order.get(b));
      const id = queue.shift();
      topo.push(id);
      for (const edge of outgoing.get(id)) {
        indegree.set(edge.to, indegree.get(edge.to) - 1);
        if (indegree.get(edge.to) === 0) queue.push(edge.to);
      }
    }
    for (const id of byId.keys()) if (!topo.includes(id)) topo.push(id);

    const rank = new Map([...byId.keys()].map((id) => [id, 1]));
    for (const id of topo) {
      for (const edge of outgoing.get(id))
        rank.set(edge.to, Math.max(rank.get(edge.to), rank.get(id) + 1));
    }
    project.layoutDepth = 0;
    for (const [id, node] of byId) node.layoutDepth = rank.get(id);

    const layers = new Map([[0, [project]]]);
    for (const id of topo) {
      const depth = rank.get(id);
      if (!layers.has(depth)) layers.set(depth, []);
      layers.get(depth).push(byId.get(id));
    }
    const levelSize = [];
    for (const [depth, nodes] of layers)
      levelSize[depth] = Math.max(...nodes.map((n) => horizontal ? n.w : n.h));

    const mainAt = [];
    let mainCursor = 0;
    for (let depth = 0; depth < levelSize.length; depth++) {
      mainAt[depth] = mainCursor;
      mainCursor += levelSize[depth] + levelGap;
    }

    const crossSize = (node) => horizontal ? node.h : node.w;
    let maxCross = 0;
    const totals = new Map();
    for (const [depth, nodes] of layers) {
      const total = nodes.reduce((sum, node) => sum + crossSize(node), 0)
        + Math.max(0, nodes.length - 1) * siblingGap;
      totals.set(depth, total);
      maxCross = Math.max(maxCross, total);
    }
    for (const [depth, nodes] of layers) {
      let crossCursor = (maxCross - totals.get(depth)) / 2;
      for (const node of nodes) {
        node.main = mainAt[depth] || 0;
        node.cross = crossCursor;
        crossCursor += crossSize(node) + siblingGap;
      }
    }

    for (const node of all) {
      node.x = horizontal ? node.main : node.cross;
      node.y = horizontal ? node.cross : node.main;
    }

    const maxX = all.length ? Math.max(...all.map((n) => n.x + n.w)) : 0;
    const maxY = all.length ? Math.max(...all.map((n) => n.y + n.h)) : 0;
    return { all, edges: graphEdges, maxX, maxY, minX: 0, minY: 0, orientation };
  }

  function connector(parent, child, horizontal) {
    if (horizontal) {
      const x1 = parent.x + parent.w;
      const y1 = parent.y + parent.h / 2;
      const x2 = child.x;
      const y2 = child.y + child.h / 2;
      const mid = x1 + (x2 - x1) / 2;
      return `M${x1},${y1} H${mid} V${y2} H${x2}`;
    }
    const x1 = parent.x + parent.w / 2;
    const y1 = parent.y + parent.h;
    const x2 = child.x + child.w / 2;
    const y2 = child.y;
    const mid = y1 + (y2 - y1) / 2;
    return `M${x1},${y1} V${mid} H${x2} V${y2}`;
  }

  function edgeLabel(edge, horizontal) {
    if (!["pass", "fail"].includes(edge.when)) return "";
    const text = edge.label || edge.when.toUpperCase();
    const x = horizontal
      ? (edge.parent.x + edge.parent.w + edge.child.x) / 2
      : edge.child.x + edge.child.w / 2 + 8;
    const y = horizontal
      ? edge.child.y + edge.child.h / 2 - 7
      : (edge.parent.y + edge.parent.h + edge.child.y) / 2 - 7;
    return `<text class="edge-label edge-label-${esc(edge.when)}" x="${x}" y="${y}">${esc(text)}</text>`;
  }

  function nodeSvg(node) {
    const progress = node.progress === null || node.progress === undefined ? null : node.progress;
    const percent = progress === null ? "—" : `${(progress * 100).toFixed(0)}%`;
    const classes = [
      "mnode",
      `m-${node.status}`,
      node.isProject ? "m-project" : "",
      node.is_current ? "m-current" : "",
      node.selected ? "m-selected" : "",
    ].filter(Boolean).join(" ");

    const identity = node.isProject
      ? `role="group" aria-label="${esc(`Project ${node.name}, ${percent}`)}"`
      : `data-id="${esc(node.id)}" role="button" tabindex="0"
         aria-label="${esc(`${node.id} ${node.name}, ${statusLabel(node.status)}, ${percent}`)}"`;

    return `<g class="${classes}" ${identity} transform="translate(${node.x},${node.y})">
      <rect class="mbox" width="${node.w}" height="${node.h}" rx="7"/>
      <rect class="mrail" width="4" height="${node.h}" rx="2"/>
      <foreignObject x="4" width="${node.w - 4}" height="${node.h}">
        <div xmlns="http://www.w3.org/1999/xhtml" class="mbody">
          <div class="mtop">
            <span class="mid">${node.isProject ? "PROJECT" : esc(node.id)}</span>
            <span class="mpct">${percent}</span>
          </div>
          <div class="mname">${esc(node.name)}</div>
          <div class="mmeta">${node.isProject ? "Current accepted plan" :
            `${node.is_current ? "Current · " : ""}${esc(node.kind || "task")} · ${esc(statusLabel(node.status))} · ${esc(String(node.outcome || "active").replace("_", " "))}`}</div>
        </div>
      </foreignObject>
    </g>`;
  }

  function render(state, opts) {
    const { orientation = "lr", selected, onSelect } = opts;
    const horizontal = orientation === "lr";
    const layout = computeLayout(state, orientation);
    for (const node of layout.all) node.selected = node.id === selected;
    onSelectRef = onSelect;

    const host = document.getElementById("map");
    if (!layout.all.length) {
      host.innerHTML = `<div class="tree-empty">No roadmap nodes yet.</div>`;
      return layout;
    }

    host.innerHTML = `<svg id="map-svg" width="100%" height="100%"
      role="img" aria-label="Hierarchical roadmap">
      <g id="map-camera">
        ${layout.edges.map((edge) =>
          `<path class="edge edge-${esc(edge.child.status)} edge-when-${esc(edge.when)}" d="${connector(edge.parent, edge.child, horizontal)}"/>`
        ).join("")}
        ${layout.edges.map((edge) => edgeLabel(edge, horizontal)).join("")}
        ${layout.all.map(nodeSvg).join("")}
      </g>
    </svg>`;

    if (!controlsBound) {
      attachControls();
      controlsBound = true;
    }
    return layout;
  }

  function applyCamera() {
    const camera = document.getElementById("map-camera");
    if (camera) camera.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
  }

  function fit(bounds) {
    const host = document.getElementById("map");
    if (!bounds || !bounds.maxX || !bounds.maxY) return;
    const pad = 32;
    const width = host.clientWidth || 900;
    const height = host.clientHeight || 600;
    view.k = Math.min(1, Math.max(.16,
      Math.min((width - pad * 2) / bounds.maxX, (height - pad * 2) / bounds.maxY)));
    view.x = Math.max(pad, (width - bounds.maxX * view.k) / 2);
    view.y = Math.max(pad, (height - bounds.maxY * view.k) / 2);
    applyCamera();
  }

  function attachControls() {
    const host = document.getElementById("map");
    let down = null;

    host.addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = host.getBoundingClientRect();
      const mouseX = event.clientX - rect.left;
      const mouseY = event.clientY - rect.top;
      const scale = Math.min(2.5, Math.max(.15, view.k * Math.exp(-event.deltaY * .0015)));
      view.x = mouseX - (mouseX - view.x) * (scale / view.k);
      view.y = mouseY - (mouseY - view.y) * (scale / view.k);
      view.k = scale;
      applyCamera();
    }, { passive: false });

    host.addEventListener("pointerdown", (event) => {
      down = {
        x: event.clientX,
        y: event.clientY,
        originX: view.x,
        originY: view.y,
        id: event.pointerId,
        moved: false,
      };
    });

    host.addEventListener("pointermove", (event) => {
      if (!down) return;
      const dx = event.clientX - down.x;
      const dy = event.clientY - down.y;
      if (!down.moved) {
        if (Math.hypot(dx, dy) < 4) return;
        down.moved = true;
        try { host.setPointerCapture(down.id); } catch { /* already released */ }
        host.classList.add("grabbing");
      }
      view.x = down.originX + dx;
      view.y = down.originY + dy;
      applyCamera();
    });

    host.addEventListener("pointerup", (event) => {
      if (down && !down.moved) {
        const node = event.target.closest && event.target.closest(".mnode");
        if (node && node.dataset.id && onSelectRef) onSelectRef(node.dataset.id);
      }
      down = null;
      host.classList.remove("grabbing");
    });

    host.addEventListener("pointercancel", () => {
      down = null;
      host.classList.remove("grabbing");
    });

    host.addEventListener("keydown", (event) => {
      const node = event.target.closest && event.target.closest(".mnode");
      if (node && node.dataset.id && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        if (onSelectRef) onSelectRef(node.dataset.id);
      }
    });
  }

  return { render, fit, applyCamera, computeLayout, view };
})();
