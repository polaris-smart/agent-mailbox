"""agent-mailbox web board: a zero-dependency kanban UI over the task store.

Run:  ``agent-mailbox --web 8643``  →  http://127.0.0.1:8643/?token=…

Built on ``http.server`` plus one embedded HTML page — no framework, no build
step. Auth is a bearer token: set ``AGENT_MAIL_WEB_TOKEN`` for a stable one,
otherwise a fresh token is generated per boot and printed to stdout. The
token holder acts as agent ``boss``: cards created or dragged on the board go
through the normal task tools, so every move still auto-messages (and wakes)
the assignee.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import MailStore

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-mailbox · board</title>
<style>
  /* Less is more: one neutral surface + a status dot per lane.
     Three type sizes only — 15px titles, 13px body, 12px auxiliary.
     Every space is a multiple of 4 on an 8px grid.                    */
  :root {
    --bg:#f4f5f7; --panel:#ffffff; --card:#ffffff; --card-hover:#ffffff;
    --line:#e3e6ec; --line-soft:#eceef2; --text:#1d2026; --dim:#6e7585;
    --accent:#4a6fa5; --accent-ink:#ffffff;
    --shadow:0 2px 8px rgba(29,32,38,.08);
    --shadow-lift:0 4px 14px rgba(29,32,38,.12);
    /* status dots — low saturation, tuned per theme */
    --st-todo:#9aa1af; --st-doing:#5d81b8; --st-review:#b8904f; --st-done:#629c72;
    --over-bg:#f3f6fb;
    --badge-bg:#eef0f4; --badge-ink:#5a6172;
    --danger:#b4524e; --danger-bg:#faf1f0; --danger-line:#e6cfcd;
  }
  html[data-theme="dark"] {
    --bg:#101216; --panel:#171a20; --card:#1d2129; --card-hover:#222630;
    --line:#282d38; --line-soft:#21252e; --text:#e3e6ee; --dim:#8a91a4;
    --accent:#7195cd; --accent-ink:#101216;
    --shadow:0 2px 8px rgba(0,0,0,.35);
    --shadow-lift:0 4px 16px rgba(0,0,0,.5);
    --st-todo:#828a9c; --st-doing:#7f9fda; --st-review:#c7a266; --st-done:#7db389;
    --over-bg:#1a2029;
    --badge-bg:#242935; --badge-ink:#98a0b2;
    --danger:#d4807c; --danger-bg:#2a2021; --danger-line:#4a3234;
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text);
         font:13px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
         min-height:100vh; display:flex; flex-direction:column;
         -webkit-font-smoothing:antialiased; }
  h1, .card b { font-size:15px; font-weight:600; }
  .aux { font-size:12px; color:var(--dim); }

  header { display:flex; gap:8px 16px; align-items:baseline; padding:16px 24px;
           border-bottom:1px solid var(--line); flex-wrap:wrap; }
  header h1 { letter-spacing:-.01em; }
  header .dim { color:var(--dim); font-size:12px; }
  header .spacer { flex:1; }
  form { display:flex; gap:8px; padding:16px 24px; flex-wrap:wrap; }
  input { background:var(--panel); color:var(--text); border:1px solid var(--line);
          border-radius:6px; padding:6px 12px; font:inherit; }
  input::placeholder { color:var(--dim); }
  input:focus { outline:none; border-color:var(--accent); }

  #board { display:grid; grid-template-columns:repeat(4,1fr); gap:16px;
           padding:16px 24px 32px; flex:1; align-items:start; }
  .lane { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:8px; min-height:180px;
          transition:background-color .12s, border-color .12s; }
  .lane h2 { font-size:12px; font-weight:600; text-transform:uppercase;
             letter-spacing:.06em; color:var(--dim);
             padding:8px 8px 12px; display:flex; align-items:center; gap:8px; }
  .lane h2 .dot { width:8px; height:8px; border-radius:50%; flex:none; }
  .lane[data-status="todo"]   .dot { background:var(--st-todo); }
  .lane[data-status="doing"]  .dot { background:var(--st-doing); }
  .lane[data-status="review"] .dot { background:var(--st-review); }
  .lane[data-status="done"]   .dot { background:var(--st-done); }
  .lane h2 .n { margin-left:auto; font-size:12px; font-weight:500;
                color:var(--badge-ink); background:var(--badge-bg);
                border-radius:10px; padding:0 8px; line-height:20px;
                letter-spacing:0; min-width:12px; text-align:center; }
  .lane .empty { color:var(--dim); font-size:12px; text-align:center;
                 padding:16px 0; opacity:.6; }
  .lane.over { border-color:var(--accent); background:var(--over-bg); }
  .lane.over h2 { color:var(--accent); }

  .card { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:12px 16px; margin-bottom:8px; cursor:grab;
          transition:box-shadow .15s ease, transform .15s ease, border-color .15s ease; }
  .card:hover { transform:translateY(-1px); border-color:var(--dim);
                box-shadow:var(--shadow); background:var(--card-hover); }
  .card.dragging { opacity:.5; transform:rotate(1deg) scale(1.01);
                   box-shadow:var(--shadow-lift); cursor:grabbing; }
  .card b { display:block; word-break:break-word; }
  .card .meta { font-size:12px; color:var(--dim); margin-top:8px;
                display:flex; align-items:center; gap:8px; }
  .card .who { width:20px; height:20px; border-radius:50%; flex:none;
               background:var(--badge-bg); color:var(--badge-ink);
               font-size:12px; line-height:20px; text-align:center;
               font-weight:600; letter-spacing:.02em; }
  .card .due { margin-left:auto; }
  .card .due.late { color:var(--danger); }

  button { background:var(--accent); color:var(--accent-ink); border:0;
           border-radius:6px; padding:6px 14px; font:inherit; cursor:pointer; }
  button:hover { filter:brightness(1.06); }
  button.ghost { background:transparent; color:var(--dim);
                 border:1px solid var(--line); }
  button.ghost:hover { color:var(--text); border-color:var(--dim); filter:none; }

  #toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%);
           background:var(--danger-bg); color:var(--danger);
           border:1px solid var(--danger-line);
           border-radius:8px; padding:8px 16px; font-size:13px; display:none;
           max-width:80vw; box-shadow:var(--shadow); }
</style>
</head>
<body>
<header>
  <h1>agent-mailbox · board</h1>
  <span class="dim">drag between adjacent lanes · every move messages the assignee</span>
  <span class="spacer"></span>
  <button id="theme" class="ghost" title="toggle light / dark">◐</button>
  <button id="refresh" class="ghost">refresh</button>
</header>
<form id="new">
  <input name="title" placeholder="task title" required size="32">
  <input name="assignee" placeholder="assignee (e.g. ZC)" required size="14">
  <input name="due" placeholder="due (e.g. 2026-09-10)" size="16">
  <button>create</button>
</form>
<div id="board">
  <div class="lane" data-status="todo"><h2><span class="dot"></span>todo <span class="n"></span></h2></div>
  <div class="lane" data-status="doing"><h2><span class="dot"></span>doing <span class="n"></span></h2></div>
  <div class="lane" data-status="review"><h2><span class="dot"></span>review <span class="n"></span></h2></div>
  <div class="lane" data-status="done"><h2><span class="dot"></span>done <span class="n"></span></h2></div>
</div>
<div id="toast"></div>
<script>
const token = new URLSearchParams(location.search).get("token") || localStorage.getItem("mb_token") || "";
localStorage.setItem("mb_token", token);
const hdr = { "Authorization": "Bearer " + token, "Content-Type": "application/json" };

/* theme: ?theme=… wins (no persistence), then the toggle's saved choice,
   then the OS preference. Light and dark tokens are tuned separately. */
const rootEl = document.documentElement;
const qTheme = new URLSearchParams(location.search).get("theme");
const savedTheme = localStorage.getItem("mb_theme");
rootEl.dataset.theme = qTheme || savedTheme
  || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
document.getElementById("theme").addEventListener("click", () => {
  const next = rootEl.dataset.theme === "dark" ? "light" : "dark";
  rootEl.dataset.theme = next;
  localStorage.setItem("mb_theme", next);
});

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.display = "block";
  setTimeout(() => t.style.display = "none", 4000);
}

async function api(path, body) {
  const res = await fetch(path, body === undefined
    ? { headers: hdr }
    : { method: "POST", headers: hdr, body: JSON.stringify(body) });
  if (!res.ok) {
    let detail = res.status + " " + res.statusText;
    try { detail = (await res.json()).error || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function cardEl(t) {
  const el = document.createElement("div");
  el.className = "card"; el.draggable = true; el.dataset.id = t.id;
  const late = t.due && t.status !== "done" && t.due < new Date().toISOString().slice(0, 10);
  const initials = esc((t.assignee || "?").slice(0, 2).toUpperCase());
  el.innerHTML = `<b>${esc(t.title)}</b>
    <div class="meta"><span class="who" title="@${esc(t.assignee)}">${initials}</span>
    <span>#${esc(t.id)}</span>
    ${t.due ? `<span class="due${late ? " late" : ""}">${esc(t.due)}</span>` : ""}</div>`;
  el.addEventListener("dragstart", e => {
    e.dataTransfer.setData("text/plain", t.id); el.classList.add("dragging");
  });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  return el;
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

function render(tasks) {
  const lanes = {};
  document.querySelectorAll(".lane").forEach(l => {
    lanes[l.dataset.status] = l;
    l.querySelectorAll(".card, .empty").forEach(c => c.remove());
  });
  for (const t of tasks) (lanes[t.status] || lanes.todo).appendChild(cardEl(t));
  document.querySelectorAll(".lane").forEach(l => {
    const n = l.querySelectorAll(".card").length;
    l.querySelector(".n").textContent = n;
    if (!n) { const e = document.createElement("div"); e.className = "empty"; e.textContent = "—"; l.appendChild(e); }
  });
}

async function reload() { render((await api("/api/tasks")).tasks); }

document.querySelectorAll(".lane").forEach(lane => {
  lane.addEventListener("dragover", e => e.preventDefault());
  lane.addEventListener("dragenter", () => lane.classList.add("over"));
  lane.addEventListener("dragleave", () => lane.classList.remove("over"));
  lane.addEventListener("drop", async e => {
    e.preventDefault(); lane.classList.remove("over");
    const id = e.dataTransfer.getData("text/plain");
    try { await api(`/api/tasks/${id}/move`, { status: lane.dataset.status }); await reload(); }
    catch (err) { toast(err.message); await reload(); }
  });
});

document.getElementById("new").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/tasks", { title: f.title.value, assignee: f.assignee.value, due: f.due.value });
    f.reset(); await reload();
  } catch (err) { toast(err.message); }
});

document.getElementById("refresh").addEventListener("click", reload);
setInterval(() => { if (document.visibilityState === "visible") reload().catch(() => {}); }, 5000);
reload().catch(err => toast("load failed: " + err.message));
</script>
</body>
</html>
"""


class _BoardHandler(BaseHTTPRequestHandler):
    store: MailStore
    token: str

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep stdout clean; the launcher prints the URL once

    # ------------------------------------------------------------- helpers

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if hmac.compare_digest(header.encode(), f"Bearer {self.token}".encode()):
            return True
        q = parse_qs(urlparse(self.path).query)
        return hmac.compare_digest(q.get("token", [""])[0].encode(), self.token.encode())

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _deny(self) -> None:
        self._json(401, {"error": "unauthorized: pass ?token=… or Authorization: Bearer …"})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # -------------------------------------------------------------- routes

    def do_GET(self) -> None:
        if not self._authorized():
            return self._deny()
        if urlparse(self.path).path == "/api/tasks":
            return self._json(200, {"tasks": self.store.task_list()})
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if not self._authorized():
            return self._deny()
        from .store import MailboxError

        path = urlparse(self.path).path
        try:
            if path == "/api/tasks":
                data = self._body()
                task = self.store.task_create(
                    str(data.get("title", "")), str(data.get("assignee", "")),
                    "boss", str(data.get("due", "") or ""),
                )
                return self._json(200, {"task": task})
            if path.startswith("/api/tasks/") and path.endswith("/move"):
                tid = path[len("/api/tasks/"):-len("/move")]
                data = self._body()
                task = self.store.task_move(
                    tid, str(data.get("status", "")), moved_by="boss",
                    note=str(data.get("note", "") or ""),
                )
                return self._json(200, {"task": task})
        except MailboxError as e:
            return self._json(400, {"error": str(e)})
        self._json(404, {"error": f"no such endpoint: {path}"})


class LoopbackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS in HTTPServer.server_bind().

    The stock server_bind() runs socket.getfqdn("127.0.0.1") — a blocking PTR
    lookup that can blackhole >30 s on some hosts (macOS CI runners, VMs with
    slow resolvers), stalling startup for that long. Nothing in _BoardHandler
    reads server_name, so bind plainly and skip the lookup.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _web_token(root: str | os.PathLike[str]) -> str:
    """Bearer token for the board: ``AGENT_MAIL_WEB_TOKEN`` wins; otherwise
    load (or create, 0600) ``<root>/web_token`` so the token survives reboots
    instead of being regenerated every boot."""
    env = os.environ.get("AGENT_MAIL_WEB_TOKEN")
    if env:
        return env
    path = Path(root) / "web_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if token:
        return token
    token = secrets.token_urlsafe(16)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(path, 0o600)  # mode arg only applies at creation; re-assert
    except OSError:
        pass
    return token


def run_web(port: int = 8643, store: MailStore | None = None) -> None:
    """Serve the board on 127.0.0.1:port until interrupted."""
    store = store or MailStore()
    token = _web_token(store.root)
    handler = type("BoardHandler", (_BoardHandler,), {
        "store": store,
        "token": token,
    })
    srv = LoopbackServer(("127.0.0.1", port), handler)
    print(f"[agent-mailbox] board: http://127.0.0.1:{port}/?token={token}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
