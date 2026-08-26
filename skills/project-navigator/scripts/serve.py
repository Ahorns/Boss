"""Local dashboard server.

Reads each project's roadmap.yaml fresh on every request and computes the
payload there and then, so the page can never show state that disagrees with the
file. There is no data.json to fall out of sync and no watcher to miss an event.

Serves one project (`pnav serve`) or many (`pnav hub`) through the same routes;
the many-project case just has more entries in /api/hub.
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from compute import build_state
from model import (PnavError, load, paths, pending_plan_change, read_structure,
                   snapshot_plan, validate, write_state, write_structure)

SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD = SKILL_DIR / "dashboard"

# Explicit whitelist rather than a static file handler: the server must not be
# able to read anything outside the dashboard, whatever the URL says.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/hub": ("hub.html", "text/html; charset=utf-8"),
    "/hub.html": ("hub.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/map.js": ("map.js", "text/javascript; charset=utf-8"),
    "/hub.js": ("hub.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def snapshot(root: Path, cache: dict) -> dict:
    """Compute a project's payload, refusing to render an invalid roadmap."""
    raw = paths(root)["roadmap"]
    raw_text = raw.read_text(encoding="utf-8") if raw.is_file() else None
    doc = load(root)

    errors, warnings = validate(doc, raw_text)
    if errors:
        return {
            "error": "roadmap.yaml is invalid:\n  - " + "\n  - ".join(errors),
            "rev": "invalid:" + str(hash(tuple(errors)) & 0xFFFFFF),
            "root": str(root),
            "project": root.name,
        }

    if read_structure(root) is None:
        write_structure(root, doc)   # adopt an existing roadmap silently
    if not pending_plan_change(root, doc):
        snapshot_plan(root, doc)

    state = build_state(doc, root)
    state["warnings"] = warnings

    # state.json is a convenience snapshot for other tools, not the live feed;
    # rewriting it on every poll would churn the disk once a second for nothing.
    if cache.get(str(root)) != state["rev"]:
        write_state(root, state)
        cache[str(root)] = state["rev"]
    return state


def summarise(state: dict) -> dict:
    """The few fields the hub cards need, so the overview stays small."""
    if state.get("error"):
        return {"root": state["root"], "project": state.get("project"),
                "error": state["error"]}
    cur = state.get("current_node") or {}
    return {
        "root": state["root"],
        "project": state["project"],
        "plan_version": state.get("plan_version", 1),
        "progress": state["progress"],
        "mode": state["mode"],
        "current": state.get("current"),
        "current_name": cur.get("name"),
        "current_outcome": cur.get("outcome"),
        "next_action": cur.get("next_action"),
        "leaf_counts": state["leaf_counts"],
        "plan_change": bool(state.get("plan_change")),
        "proposals": sum(1 for p in state.get("proposals") or []
                         if p.get("status") == "proposed"),
        "warnings": len(state.get("warnings") or []),
        "rev": state["rev"],
    }


def make_handler(roots: list[Path]):
    cache: dict = {}
    known = {str(r): r for r in roots}

    def resolve(query: str) -> Path:
        p = urllib.parse.parse_qs(query).get("p", [None])[0]
        return known.get(p, roots[0]) if p else roots[0]

    class Handler(BaseHTTPRequestHandler):
        server_version = "pnav"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload) -> None:
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")

            if path == "/api/state":
                root = resolve(query)
                try:
                    self._json(snapshot(root, cache))
                except PnavError as exc:
                    self._json({"error": str(exc), "rev": "error", "root": str(root)})
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {exc}",
                                "rev": "error", "root": str(root)})
                return

            if path == "/api/hub":
                out = []
                for r in roots:
                    try:
                        out.append(summarise(snapshot(r, cache)))
                    except PnavError as exc:
                        out.append({"root": str(r), "project": r.name, "error": str(exc)})
                    except Exception as exc:  # noqa: BLE001
                        out.append({"root": str(r), "project": r.name,
                                    "error": f"{type(exc).__name__}: {exc}"})
                self._json({"projects": out, "multi": len(roots) > 1})
                return

            entry = STATIC.get(path)
            if entry is None:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            fname, ctype = entry
            self._send(200, (DASHBOARD / fname).read_bytes(), ctype)

        def log_message(self, fmt, *args) -> None:
            pass  # the poll loop would otherwise print a line every second

    return Handler


def run(roots, host: str = "127.0.0.1", port: int = 8765) -> None:
    roots = [Path(r) for r in roots]
    if not roots:
        raise PnavError("no projects to serve.")

    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(roots))
    except OSError as exc:
        raise PnavError(
            f"cannot bind {host}:{port} ({exc}).\n"
            f"  Another dashboard may already be running - try --port {port + 1}."
        ) from exc

    if len(roots) == 1:
        print(f"project-navigator  {roots[0]}")
        print(f"  http://{host}:{port}")
    else:
        print(f"project-navigator  {len(roots)} projects")
        for r in roots:
            print(f"  - {r}")
        print(f"  http://{host}:{port}/hub")
    print("  Ctrl-C to stop", flush=True)   # so `nohup pnav hub &` logs something

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
