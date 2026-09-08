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

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import MailStore

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-mailbox · board</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --card:#1f242e; --line:#2a303c;
          --text:#e6e9ef; --dim:#8b93a3; --accent:#4f8cff; }
  * { box-sizing: border-box; margin: 0; }
  body { background:var(--bg); color:var(--text);
         font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
         min-height:100vh; display:flex; flex-direction:column; }
  header { display:flex; gap:12px; align-items:center; padding:12px 20px;
           border-bottom:1px solid var(--line); flex-wrap:wrap; }
  header h1 { font-size:16px; font-weight:600; }
  header .dim { color:var(--dim); font-size:12px; }
  header button { margin-left:auto; }
  form { display:flex; gap:8px; padding:10px 20px; flex-wrap:wrap; }
  input { background:var(--panel); color:var(--text); border:1px solid var(--line);
          border-radius:6px; padding:6px 10px; font:inherit; }
  input::placeholder { color:var(--dim); }
  #board { display:grid; grid-template-columns:repeat(4,1fr); gap:12px;
           padding:12px 20px 24px; flex:1; align-items:start; }
  .lane { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:10px; min-height:160px; }
  .lane h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
             color:var(--dim); padding:2px 4px 10px; display:flex; justify-content:space-between; }
  .lane.over { outline:2px dashed var(--accent); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:10px; margin-bottom:8px; cursor:grab; }
  .card b { display:block; font-weight:600; word-break:break-word; }
  .card .meta { color:var(--dim); font-size:12px; margin-top:6px;
                display:flex; justify-content:space-between; gap:6px; }
  .card .due.late { color:#ff7a7a; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px;
           padding:6px 14px; font:inherit; cursor:pointer; }
  #toast { position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
           background:#3a1d1d; color:#ffb4b4; border:1px solid #6b2c2c;
           border-radius:8px; padding:8px 16px; display:none; max-width:80vw; }
</style>
</head>
<body>
<header>
  <h1>agent-mailbox · board</h1>
  <span class="dim">drag between adjacent lanes · every move messages the assignee</span>
  <button id="refresh">refresh</button>
</header>
<form id="new">
  <input name="title" placeholder="task title" required size="32">
  <input name="assignee" placeholder="assignee (e.g. ZC)" required size="14">
  <input name="due" placeholder="due (e.g. 2026-09-10)" size="16">
  <button>create</button>
</form>
<div id="board">
  <div class="lane" data-status="todo"><h2>todo <span class="n"></span></h2></div>
  <div class="lane" data-status="doing"><h2>doing <span class="n"></span></h2></div>
  <div class="lane" data-status="review"><h2>review <span class="n"></span></h2></div>
  <div class="lane" data-status="done"><h2>done <span class="n"></span></h2></div>
</div>
<div id="toast"></div>
<script>
const token = new URLSearchParams(location.search).get("token") || localStorage.getItem("mb_token") || "";
localStorage.setItem("mb_token", token);
const hdr = { "Authorization": "Bearer " + token, "Content-Type": "application/json" };

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
  el.innerHTML = `<b>${esc(t.title)}</b>
    <div class="meta"><span>@${esc(t.assignee)}</span>
    ${t.due ? `<span class="due${late ? " late" : ""}">${esc(t.due)}</span>` : ""}</div>`;
  el.addEventListener("dragstart", e => e.dataTransfer.setData("text/plain", t.id));
  return el;
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

function render(tasks) {
  const lanes = {};
  document.querySelectorAll(".lane").forEach(l => { lanes[l.dataset.status] = l; l.querySelectorAll(".card").forEach(c => c.remove()); });
  for (const t of tasks) (lanes[t.status] || lanes.todo).appendChild(cardEl(t));
  document.querySelectorAll(".lane").forEach(l => {
    l.querySelector(".n").textContent = l.querySelectorAll(".card").length;
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
        if header == f"Bearer {self.token}":
            return True
        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [""])[0] == self.token

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


def run_web(port: int = 8643, store: MailStore | None = None) -> None:
    """Serve the board on 127.0.0.1:port until interrupted."""
    token = os.environ.get("AGENT_MAIL_WEB_TOKEN") or secrets.token_urlsafe(16)
    handler = type("BoardHandler", (_BoardHandler,), {
        "store": store or MailStore(),
        "token": token,
    })
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"[agent-mailbox] board: http://127.0.0.1:{port}/?token={token}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
