"""agent-mailbox web board: a zero-dependency kanban UI over the task store.

Run:  ``agent-mailbox --web 8643``  →  http://127.0.0.1:8643/?token=…

Built on ``http.server`` plus embedded HTML pages — no framework, no build
step. Auth is a bearer token: set ``AGENT_MAIL_WEB_TOKEN`` for a stable one,
otherwise a fresh token is generated per boot and printed to stdout. The
token holder acts as agent ``boss``: cards created or dragged on the board go
through the normal task tools, so every move still auto-messages (and wakes)
the assignee.

v0.7.5 adds the human product surface (任务书 §4), all under the same token:

- ``/mail``       三栏真邮箱 (§4.2): folders / monitoring / members / actions,
                  plus the §3.6 empty-mailbox call-to-action.
- ``/setup``      3 步接入向导 (§4.1), backed by discover.py (L1–L3 scan,
                  L4 test letters) — real engine calls, injectable in tests.
- ``/visibility`` 可见性页 (§4.3): the four §3.3 switches + audit trail.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socketserver
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import (
    ATTENTION_TIERS,
    HARD_OFF_VISIBILITY,
    VISIBILITY_DEFAULTS,
    MailboxError,
    MailStore,
    load_visibility,
    redact_sealed,
)
from .webpages import MAILBOX_PAGE, SETUP_PAGE, VISIBILITY_PAGE

# The human acts on the mail root as this owner id (§3.3: 人是主人). It is in
# OWNER_IDS, so its default kind is "owner" — the confirmation/audit gates
# accept it without extra registration.
OWNER_ID = "boss"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-mailbox · board</title>
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png">
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
  /* brand lockup: 40px (>32px floor for the radio-wave mark); swap art per theme */
  header h1 .logo { height:40px; vertical-align:middle; }
  header h1 .logo.dark { display:none; }
  html[data-theme="dark"] header h1 .logo:not(.dark) { display:none; }
  html[data-theme="dark"] header h1 .logo.dark { display:inline; }
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
  <h1><img class="logo" src="/brand/lockup-h-light.svg" alt="agent-mailbox · board">
      <img class="logo dark" src="/brand/lockup-h-dark.svg" alt="" aria-hidden="true">
      <span class="dim">· board</span></h1>
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


def _brand_dir() -> Path | None:
    """Locate assets/brand/: repo checkout (editable install) or cwd fallback."""
    here = Path(__file__).resolve()
    for base in (*here.parents, Path.cwd()):
        d = base / "assets" / "brand"
        if (d / "favicon.ico").is_file():
            return d
    return None


# ------------------------------------------------------------ owner state
# Per-root UI state (stars / read marks for monitored letters / drafts). This
# is deliberately OUTSIDE the letter files: the human must not modify other
# members' letters (§3.3 人的动作边界), so monitoring read/star state lives in
# our own <root>/web_state.json instead of in the letters themselves.


def _load_web_state(root: str | os.PathLike[str]) -> dict:
    try:
        data = json.loads((Path(root) / "web_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "starred": [str(x) for x in (data.get("starred") or [])],
        "read": [str(x) for x in (data.get("read") or [])],
        "drafts": [d for d in (data.get("drafts") or []) if isinstance(d, dict)],
    }


def _save_web_state(root: str | os.PathLike[str], state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=Path(root), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, Path(root) / "web_state.json")


def _state_toggle(root: str | os.PathLike[str], key: str, msg_id: str, add: bool) -> dict:
    state = _load_web_state(root)
    ids = [x for x in state[key] if x != msg_id]
    if add:
        ids.append(msg_id)
    state[key] = ids
    _save_web_state(root, state)
    return state


# ---------------------------------------------------------- discover glue
# The wizard and the 体检 view call the real discover.py engine. Module-level
# indirection keeps tests hermetic: they monkeypatch _run_discover/_run_test_letter
# instead of probing the real machine. Reports are memoized per root for 60 s
# so the mailbox polling never re-runs lsof/ps on every refresh.

_REPORT_TTL = 60.0
_REPORTS: dict[str, dict] = {}


def _run_discover(root: str | os.PathLike[str], *, save: bool) -> dict:
    from .discover import build_report, default_context

    report = build_report(default_context(Path(root)), save=save)
    _REPORTS[str(Path(root))] = {"at": time.monotonic(), "report": report}
    return report


def _cached_report(root: str | os.PathLike[str]) -> dict | None:
    hit = _REPORTS.get(str(Path(root)))
    if hit and time.monotonic() - hit["at"] <= _REPORT_TTL:
        return hit["report"]
    return None


def _run_test_letter(root: str | os.PathLike[str], member: str, timeout: float) -> dict:
    from .discover import test_member

    return test_member(member, Path(root), timeout=timeout)


def _undeliverable_members(report: dict) -> set[str]:
    """Members whose every known wake channel is broken — letters addressed to
    them are tagged 未送达 (§4.2). Empty for members with no channels: no
    channel found ≠ channel broken, and the brief forbids guessing."""
    bad: set[str] = set()
    for m in report.get("members", []):
        chans = m.get("channels") or []
        if chans and all(c.get("status") == "broken" for c in chans):
            bad.add(str(m.get("member")))
    return bad


def _member_cards(store: MailStore) -> list[dict]:
    """左栏「成员」名单: registry + boss + 已发现未注册的成员，each with a
    channel-status dot from the cached discover report (idle=未实测 when no
    scan has run yet). Discovered-but-unregistered members still show up —
    the roster must reflect what discovery found, not just who registered."""
    report = _cached_report(store.root) or {}
    channels = {m.get("member"): (m.get("channels") or []) for m in report.get("members", [])}
    reg = store.registry().get("agents", {})
    out = []
    for aid in sorted(set(reg) | {OWNER_ID} | set(channels)):
        card = reg.get(aid) if isinstance(reg.get(aid), dict) else {}
        chans = channels.get(aid) or []
        statuses = [c.get("status") for c in chans]
        if statuses and all(s == "broken" for s in statuses):
            dot = "bad"
        elif "broken" in statuses:
            dot = "warn"
        elif "ok" in statuses:
            dot = "ok"
        else:
            dot = "idle"
        out.append(
            {
                "id": aid,
                "kind": store.kind_of(aid),
                "description": card.get("description", ""),
                "dot": dot,
                "channels": chans,
            }
        )
    return out


def _mailbox_payload(store: MailStore) -> dict:
    """GET /api/mail — everything the 三栏 mailbox renders in one call.

    Sealed letters are redacted here (metadata only — never a human-view
    body, §3.3); monitored letters carry read/star state from web_state.json
    (the human never edits other members' letters); ``undeliverable`` comes
    from the cached discover report when one has been run.
    """
    owner = OWNER_ID
    state = _load_web_state(store.root)
    starred = set(state["starred"])
    read_marks = set(state["read"])
    report = _cached_report(store.root) or {}
    bad = _undeliverable_members(report)
    letters = []
    for raw in store.owner_view():
        m = redact_sealed(dict(raw), reader=owner)
        mid = str(m.get("id", ""))
        to = str(m.get("to", ""))
        frm = str(m.get("from", ""))
        mine_to = to == owner
        if mine_to:
            read = m.get("status") != "pending"
        elif frm == owner:
            read = True  # 自己写的信没有未读一说
        else:
            read = mid in read_marks
        letters.append(
            {
                **m,
                "monitor": not mine_to and frm != owner,
                "starred": mid in starred,
                "read": read,
                "undeliverable": to in bad,
            }
        )
    counts = {
        "inbox": sum(1 for x in letters if x["to"] == owner and not x["_archived"]),
        "sent": sum(1 for x in letters if x["from"] == owner),
        "archived": sum(1 for x in letters if x["to"] == owner and x["_archived"]),
        "monitor": sum(1 for x in letters if x["monitor"]),
        "undelivered": sum(1 for x in letters if x["undeliverable"]),
        "sealed": sum(1 for x in letters if x.get("redacted") == "sealed"),
        "starred": len(starred),
        "drafts": len(state["drafts"]),
    }
    attention_unread = sum(
        1
        for x in letters
        if x["to"] == owner
        and not x["_archived"]
        and x.get("status") == "pending"
        and x.get("attention") == "decision"
    )
    return {
        "owner": owner,
        "letters": letters,
        "members": _member_cards(store),
        "starred": state["starred"],
        "drafts": state["drafts"],
        "counts": counts,
        "attention_unread": attention_unread,
    }


_MAIL_ACTION_RE = re.compile(
    r"^/api/mail/([^/]+)/(read|unread|archive|star|unstar|task|confirm-external)$"
)


# Brand assets under the repo's assets/brand/: explicit allow-list only (no
# globbing, no path traversal). Public on purpose — favicons and the header
# logo must load before/without the bearer token.
_BRAND_FILES: dict[str, tuple[str, str]] = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/brand/badge.svg": ("svg/badge.svg", "image/svg+xml"),
    "/brand/lockup-h-light.svg": ("svg/lockup-h-light.svg", "image/svg+xml"),
    "/brand/lockup-h-dark.svg": ("svg/lockup-h-dark.svg", "image/svg+xml"),
    "/brand/mark-light.svg": ("svg/mark-light.svg", "image/svg+xml"),
    "/brand/mark-dark.svg": ("svg/mark-dark.svg", "image/svg+xml"),
    "/brand/apple-touch-icon.png": ("png/icon-180.png", "image/png"),
}


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

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _deny(self) -> None:
        self._json(401, {"error": "unauthorized: pass ?token=… or Authorization: Bearer …"})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _brand(self, path: str) -> None:
        entry = _BRAND_FILES.get(path)
        brand_dir = _brand_dir()
        if entry is None or brand_dir is None:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        name, ctype = entry
        f = brand_dir / name
        if not f.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        self._send(200, f.read_bytes(), ctype)

    # -------------------------------------------------------------- routes

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in _BRAND_FILES:
            return self._brand(path)
        if path.startswith("/brand/"):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        if not self._authorized():
            return self._deny()
        path = urlparse(self.path).path
        # v0.7.5 human pages (§4) — same token gate as the board
        if path == "/":
            # 人是主人：根路径直接进三栏信箱（旧看板挪 /board，启动日志同改）。
            # Token 透传：看板/书签的 /?token=… 跳过来时必须带上，否则 401。
            qs = urlparse(self.path).query
            return self._redirect(f"/mail?{qs}" if qs else "/mail")
        if path == "/board":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/mail":
            return self._send(200, MAILBOX_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/setup":
            return self._send(200, SETUP_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/visibility":
            return self._send(200, VISIBILITY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/tasks":
            return self._json(200, {"tasks": self.store.task_list()})
        # v0.7.5 human (owner) view of the mail itself: every letter under the
        # root. 密封信 bodies never appear here — metadata only (§3.3), so
        # "主人全可见" cannot become a credential leak channel.
        if path == "/api/messages":
            return self._json(
                200, {"messages": [redact_sealed(m) for m in self.store.all_letters()]}
            )
        if path.startswith("/api/messages/"):
            try:
                m = self.store.find_letter(path[len("/api/messages/") :])
            except MailboxError as e:
                return self._json(404, {"error": str(e)})
            return self._json(200, {"message": redact_sealed(m)})
        # ---- v0.7.5 mailbox / wizard / visibility APIs ----
        if path == "/api/mail":
            return self._json(200, _mailbox_payload(self.store))
        if path == "/api/visibility":
            return self._json(
                200,
                {
                    "visibility": load_visibility(self.store.root),
                    "defaults": dict(VISIBILITY_DEFAULTS),
                    "hard_locked": sorted(HARD_OFF_VISIBILITY),
                    "audit": self.store.audit_entries("visibility_change", limit=10),
                },
            )
        if path == "/api/status":
            # 体检/向导复用：真实调 discover 引擎（save=False，不重写指纹）
            return self._json(200, _run_discover(self.store.root, save=False))
        if path == "/api/setup-summary":
            return self._json(200, self._setup_summary())
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def _setup_summary(self) -> dict:
        """向导第 3 步「完事」：服务状态 + 接入数 + 待修项 + 模型口径（§4.1）。"""
        from .wake import WakeConfig

        report = _cached_report(self.store.root)
        if report is None:
            report = _run_discover(self.store.root, save=False)
        reg = self.store.registry().get("agents", {})
        broken = [
            {
                "member": m.get("member"),
                "reason": c.get("reason", ""),
                "next_step": c.get("next_step", ""),
            }
            for m in report.get("members", [])
            for c in (m.get("channels") or [])
            if c.get("status") == "broken"
        ]
        cfg = WakeConfig.load(Path(self.store.root))
        wake: dict = {"configured": cfg is not None}
        if cfg is not None:
            wake.update(agent_id=cfg.agent_id, adapter=cfg.adapter)
        return {
            "root": str(self.store.root),
            "generated_at_local": report.get("generated_at_local"),
            "registered": len(reg),
            "discovered": len(report.get("members", [])),
            "members": [
                {
                    "member": m.get("member"),
                    "kind": m.get("kind"),
                    "connected": bool(m.get("connected")),
                }
                for m in report.get("members", [])
            ],
            "broken": broken,
            "wake": wake,
            "model": {
                "auto": False,
                "label": "不自动处理来信",
                "note": "可选功能——收发信件本身不需要任何模型",
            },
        }

    def do_POST(self) -> None:
        if not self._authorized():
            return self._deny()
        path = urlparse(self.path).path
        try:
            if path == "/api/tasks":
                data = self._body()
                task = self.store.task_create(
                    str(data.get("title", "")),
                    str(data.get("assignee", "")),
                    "boss",
                    str(data.get("due", "") or ""),
                )
                return self._json(200, {"task": task})
            if path.startswith("/api/tasks/") and path.endswith("/move"):
                tid = path[len("/api/tasks/") : -len("/move")]
                data = self._body()
                task = self.store.task_move(
                    tid,
                    str(data.get("status", "")),
                    moved_by="boss",
                    note=str(data.get("note", "") or ""),
                )
                return self._json(200, {"task": task})
            return self._post_mail_api(path)
        except MailboxError as e:
            return self._json(400, {"error": str(e)})
        self._json(404, {"error": f"no such endpoint: {path}"})

    # ------------------------------------------------- v0.7.5 mail APIs

    def _post_mail_api(self, path: str) -> None:
        """All v0.7.5 POST endpoints (the human acts as the owner ``boss``)."""
        store = self.store
        root = store.root
        if path == "/api/mail/send":
            data = self._body()
            to = str(data.get("to", "")).strip()
            subject = str(data.get("subject", "")).strip() or "(无主题)"
            body = str(data.get("body", ""))
            if not body.strip():
                return self._json(400, {"error": "信正文不能为空（写一句再发）"})
            attention = str(data.get("attention") or "decision")
            if attention not in ATTENTION_TIERS:
                return self._json(400, {"error": f"attention must be one of {ATTENTION_TIERS}"})
            sent = store.send(
                OWNER_ID,
                to,
                subject,
                body,
                reply_to=str(data.get("reply_to") or "") or None,
                attention=attention,
                sealed=bool(data.get("sealed")),
            )
            draft_id = str(data.get("draft_id") or "")
            if draft_id:
                self._draft_delete(draft_id)
            return self._json(200, {"delivered": sent})
        if path == "/api/mail/drafts":
            data = self._body()
            state = _load_web_state(root)
            drafts = state["drafts"]
            did = str(data.get("id") or "")
            entry = {
                "id": did or f"d-{int(time.time() * 1000)}",
                "to": str(data.get("to", "")),
                "subject": str(data.get("subject", "")),
                "body": str(data.get("body", "")),
                "attention": str(data.get("attention") or "decision"),
                "sealed": bool(data.get("sealed")),
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            }
            drafts = [d for d in drafts if d.get("id") != entry["id"]]
            drafts.insert(0, entry)
            state["drafts"] = drafts[:100]
            _save_web_state(root, state)
            return self._json(200, {"draft": entry, "count": len(state["drafts"])})
        if path == "/api/mail/drafts/delete":
            self._draft_delete(str(self._body().get("id") or ""))
            return self._json(200, {"deleted": True})
        if path == "/api/members":
            data = self._body()
            card = store.register(
                str(data.get("id", "")),
                description=str(data.get("description", "") or ""),
                kind=str(data.get("kind", "") or ""),
            )
            return self._json(200, {"member": card})
        if path == "/api/discover":
            data = self._body()
            report = _run_discover(root, save=bool(data.get("save", True)))
            return self._json(200, report)
        if path == "/api/test":
            data = self._body()
            member = str(data.get("member", "")).strip()
            if not member:
                return self._json(400, {"error": "member required"})
            try:
                timeout = float(data.get("timeout", 15.0))
            except (TypeError, ValueError):
                timeout = 15.0
            result = _run_test_letter(root, member, max(1.0, min(timeout, 120.0)))
            return self._json(200, result)
        if path == "/api/visibility":
            data = self._body()
            if not isinstance(data, dict) or not data:
                return self._json(400, {"error": "visibility changes required"})
            vis = store.set_visibility(data, by=OWNER_ID)
            return self._json(
                200,
                {
                    "visibility": vis,
                    "hard_locked": sorted(HARD_OFF_VISIBILITY),
                    "audit": store.audit_entries("visibility_change", limit=10),
                },
            )
        action = _MAIL_ACTION_RE.match(path)
        if action is None:
            return self._json(404, {"error": f"no such endpoint: {path}"})
        return self._mail_action(action.group(1), action.group(2))

    def _draft_delete(self, draft_id: str) -> None:
        if not draft_id:
            return
        state = _load_web_state(self.store.root)
        state["drafts"] = [d for d in state["drafts"] if d.get("id") != draft_id]
        _save_web_state(self.store.root, state)

    def _mail_action(self, msg_id: str, action: str) -> None:
        store = self.store
        root = store.root
        owner = OWNER_ID
        try:
            letter = store.find_letter(msg_id)
        except MailboxError as e:
            return self._json(404, {"error": str(e)})
        mine = letter.get("to") == owner
        # §3.3 人的动作边界：人只读监看 agent 之间的信——改信件本体的动作
        # 只允许在自己的（boss 的）信上；监看读点/星标走 web_state，不动信。
        if action == "star":
            state = _state_toggle(root, "starred", msg_id, True)
            return self._json(200, {"starred": msg_id in state["starred"]})
        if action == "unstar":
            state = _state_toggle(root, "starred", msg_id, False)
            return self._json(200, {"starred": msg_id in state["starred"]})
        if action == "read":
            if mine and letter.get("status") == "pending":
                store.set_status(owner, msg_id, "acked")
            else:
                _state_toggle(root, "read", msg_id, True)
            return self._json(200, {"read": True})
        if action == "unread":
            if mine:
                store.set_status(owner, msg_id, "pending")
            _state_toggle(root, "read", msg_id, False)
            return self._json(200, {"read": False})
        if action == "archive":
            if not mine:
                return self._json(
                    403,
                    {"error": "只读监看：agent 之间的信不能由人归档；要发话就写一封信或派任务卡"},
                )
            store.set_status(owner, msg_id, "done")
            n = store.archive_done(owner)
            return self._json(200, {"archived": n})
        if action == "task":
            data = self._body()
            assignee = str(data.get("assignee", "")).strip()
            if not assignee:
                return self._json(400, {"error": "assignee required"})
            title = str(letter.get("subject") or "(无主题)")[:120]
            task = store.task_create(title, assignee, owner, notify=True)
            return self._json(200, {"task": task})
        if action == "confirm-external":
            m = store.confirm_external(msg_id, by=owner)
            return self._json(200, {"message": m["id"], "origin": m.get("origin")})
        return self._json(404, {"error": f"no such action: {action}"})


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
    handler = type(
        "BoardHandler",
        (_BoardHandler,),
        {
            "store": store,
            "token": token,
        },
    )
    srv = LoopbackServer(("127.0.0.1", port), handler)
    print(f"[agent-mailbox] mailbox: http://127.0.0.1:{port}/mail?token={token}", flush=True)
    print(f"[agent-mailbox] board:   http://127.0.0.1:{port}/board?token={token}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
