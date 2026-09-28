"""Embedded HTML pages for the v0.7.5 human UI (任务书 §4).

Three pages, straight off the HS 视觉原型 (same palette --accent #4a6fa5 /
墨黑 #1d2026 / 米白 #f4f5f7, same light+dark token sets) — the prototypes are
the single visual reference, so the CSS below is theirs, lightly trimmed:

- :data:`MAILBOX_PAGE`  — 三栏真邮箱 (原型 agent-mailbox-mailbox-v4-threepane)
- :data:`SETUP_PAGE`    — 3 步接入向导 (原型 agent-mailbox-setup-wizard-v2)
- :data:`VISIBILITY_PAGE` — 可见性页 (原型 agent-mailbox-mailbox-v3 的 perm 页)

Pure inline HTML/CSS/JS on top of ``fetch`` — no framework, no build step, no
CDN. Brand assets (favicon / lockup) come from the repo's assets/brand/ via
the ``/_brand`` routes already served by web.py.
"""

MAILBOX_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-mailbox · 信箱</title>
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png">
<style>
  :root{
    --bg:#f4f5f7; --panel:#fff; --line:#e3e6ec; --line-soft:#eceef2;
    --text:#1d2026; --dim:#6e7585; --accent:#4a6fa5; --accent-ink:#fff;
    --ok:#629c72; --warn:#b8904f; --bad:#b4524e; --bad-bg:#faf1f0; --bad-line:#e6cfcd;
    --ok-bg:#f1f7f2; --ok-line:#cfe3d3; --warn-bg:#fbf6ee; --warn-line:#ecdfc7;
    --chip:#eef0f4; --chip-ink:#5a6172; --sel:#eef2f8; --star:#c9903f;
    --shadow-1:0 1px 3px rgba(29,32,38,.06); --shadow-2:0 4px 16px rgba(29,32,38,.10); --shadow-3:0 12px 40px rgba(29,32,38,.24);
  }
  html[data-theme="dark"]{
    --bg:#0f1115; --panel:#161a20; --line:#282d38; --line-soft:#21252e;
    --text:#e3e6ee; --dim:#8a91a4; --accent:#7195cd; --accent-ink:#0f1115;
    --ok:#7db389; --warn:#c7a266; --bad:#d4807c; --bad-bg:#2a2021; --bad-line:#4a3234;
    --ok-bg:#1a2029; --ok-line:#2e4436; --warn-bg:#221f18; --warn-line:#4a3f2a;
    --chip:#232833; --chip-ink:#98a0b2; --sel:#1e2733; --star:#d8a75c;
    --shadow-1:0 1px 3px rgba(0,0,0,.35); --shadow-2:0 4px 16px rgba(0,0,0,.45); --shadow-3:0 12px 40px rgba(0,0,0,.6);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--text);font:13px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
  /* 顶栏 */
  .top{display:flex;gap:12px;align-items:center;padding:10px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
  .top .logo{height:32px;vertical-align:middle}
  .top .logo.dark{display:none}
  html[data-theme="dark"] .top .logo:not(.dark){display:none}
  html[data-theme="dark"] .top .logo.dark{display:inline}
  .search{flex:1;max-width:520px;background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:6px 14px;color:var(--text);font:inherit}
  .search::placeholder{color:var(--dim)}
  .spacer{flex:1}
  .dim{color:var(--dim)} .aux{font-size:12px}
  a{color:inherit;text-decoration:none}
  button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:6px;padding:8px 12px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
  button.small{padding:4px 8px;font-size:12px}
  button.ghost{background:transparent;border-color:transparent;color:var(--dim)}
  button.ghost:hover{border-color:var(--line);color:var(--text)}
  button:disabled{opacity:.5;cursor:default}
  input,textarea,select{font:inherit;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
  input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
  /* 三栏（A10 连续自适应）：栏宽全流体——侧栏 clamp、列表 minmax、详情 1fr 带下限；
     断点只负责「少一栏」台阶（见文件尾部三档媒体查询）。 */
  .app{display:grid;grid-template-columns:clamp(200px,16vw,264px) minmax(320px,min(420px,26vw)) minmax(360px,1fr);height:calc(100vh - 53px)}
  .col{border-right:1px solid var(--line);overflow:auto;background:var(--panel)}
  .col:last-child{border-right:none;background:var(--bg)}
  .write{margin:12px 12px 8px;width:calc(100% - 24px);text-align:center;padding:8px}
  .side{padding:6px 8px 16px}
  .side .h{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);padding:12px 12px 4px;font-weight:700}
  .item{display:flex;gap:8px;align-items:center;padding:8px 12px;border-radius:6px;cursor:pointer;color:var(--text)}
  .item:hover{background:var(--sel)}
  .item.on{background:var(--sel);font-weight:600}
  .item .c{margin-left:auto;color:var(--dim);font-size:12px}
  .item .dot{width:7px;height:7px;border-radius:50%;flex:none}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
  .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)} .dot.warn{background:var(--warn)} .dot.idle{background:var(--dim)}
  .hint{color:var(--dim);font-size:11px;padding:8px 12px 16px}
  /* 提醒横幅（打扰三档：默认只提醒「需你拍板」） */
  .banner{display:none;margin:8px 12px 0;padding:8px 12px;border-radius:6px;background:var(--warn-bg);border:1px solid var(--warn-line);font-size:12px}
  /* 列表 */
  .bar{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}
  .bar .h{font-weight:600}
  .listrow{display:grid;grid-template-columns:22px 96px 1fr 60px;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line-soft);cursor:pointer;line-height:1.45}
  .listrow>div{min-width:0}  /* 标签 chip 参与nowrap行会把400px列撑出横向滚动——收住溢出 */
  .listrow:hover{background:var(--sel)}
  .listrow.on{background:var(--sel);box-shadow:inset 3px 0 0 var(--accent)}
  .listrow .from{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
  .listrow .subj{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .listrow.unread .subj::before{content:"●";color:var(--accent);font-size:9px;margin-right:5px;vertical-align:1px}
  .listrow .snip{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .listrow .time{color:var(--dim);font-size:11px;text-align:right}
  .star{color:var(--dim);cursor:pointer} .star.on{color:var(--star)}
  .tag{display:inline-block;border-radius:4px;padding:0 4px;font-size:10px;border:1px solid var(--line);color:var(--dim);margin-left:8px}
  .tag.need{border-color:var(--accent);color:var(--accent)}
  .tag.seal{border-color:var(--warn);color:var(--warn)}
  .tag.undeliver{border-color:var(--bad);color:var(--bad)}
  .tag.ext{border-color:var(--bad);color:var(--bad);background:var(--bad-bg)}
  /* 空状态行动点（§3.6） */
  .empty-cta{margin:16px 12px;padding:16px;border:1px dashed var(--line);border-radius:8px;text-align:center}
  .empty-cta .ttl{font-weight:600;margin-bottom:4px}
  .empty-cta form{display:flex;flex-direction:column;gap:8px;margin-top:12px;text-align:left}
  .empty-cta .row{display:flex;gap:8px}
  .empty-cta .row>*{flex:1;min-width:0}
  /* 正文 */
  .read{padding:16px 24px 48px;max-width:78ch}
  .read h2{font-size:18px;margin:6px 0 10px;font-weight:600;word-break:break-word}
  .read .head{display:flex;gap:12px;align-items:flex-start;padding-bottom:12px;border-bottom:1px solid var(--line)}
  .read .head .av{width:34px;height:34px;border-radius:50%;background:var(--chip);color:var(--chip-ink);display:grid;place-items:center;font-weight:700;font-size:12px;flex:none}
  .read .acts{display:flex;gap:8px;margin:12px 0 4px;flex-wrap:wrap}
  .read .body{white-space:pre-line;margin-top:12px;word-break:break-word;font-size:15px;line-height:1.6}
  .note{margin-top:16px;padding:12px;border-radius:6px;font-size:12px;border:1px solid var(--bad-line);background:var(--bad-bg)}
  .note.warn{border-color:var(--warn-line);background:var(--warn-bg)}
  .note.ok{border-color:var(--ok-line);background:var(--ok-bg)}
  code{background:var(--chip);border-radius:4px;padding:1px 5px;font-size:12px}
  .thread{border-top:1px solid var(--line);margin-top:16px;padding-top:12px}
  .thread .b{margin-bottom:12px}
  .thread .b .who{font-weight:600;font-size:12px}
  .thread .b .t{color:var(--dim);font-size:11px;margin-left:6px}
  .placeholder{color:var(--dim);padding:48px 24px;text-align:center}
  /* 写信 */
  .compose form{display:flex;flex-direction:column;gap:10px;margin-top:12px}
  .compose label{font-size:12px;color:var(--dim);display:block;margin-bottom:3px}
  .compose .row{display:flex;gap:10px} .compose .row>*{flex:1;min-width:0}
  .compose textarea{min-height:140px;resize:vertical;font:inherit}
  .compose .foot{display:flex;gap:8px;margin-top:4px}
  .kv{font-size:12px;color:var(--dim);margin-top:8px}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line-soft);vertical-align:top}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:600}
  .me .av{width:24px;height:24px;border-radius:50%;background:var(--accent);color:var(--accent-ink);display:grid;place-items:center;font-size:11px;font-weight:700}
  /* 件五-B：三态齐（focus 可见 / active 反馈）——键盘能走完全流程 */
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  button:active{filter:brightness(.96)}
  /* 件五-B④：详情空状态 = 一句说明 + 一个行动按钮，不再白屏 */
  .reader-empty{padding:56px 24px;text-align:center}
  .reader-empty svg{width:40px;height:40px;color:var(--dim);opacity:.6}
  .reader-empty .ttl{font-weight:600;margin:12px 0 4px}
  .reader-empty .dim2{color:var(--dim);font-size:12px}
  .reader-empty button{margin-top:16px}
  /* 件五-D：回执链（handled_log 时间线，小字弱色，与信件 JSON 逐字一致） */
  .rc{margin-top:16px;border-top:1px solid var(--line-soft);padding-top:12px}
  .rc .h{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:600;margin-bottom:8px}
  .rc-state{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;border:1px solid var(--line);margin-left:8px;text-transform:none;letter-spacing:0}
  .rc-state.waking{border-color:var(--warn);color:var(--warn)}
  .rc-state.done{border-color:var(--ok);color:var(--ok)}
  .rc-row{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:12px;color:var(--dim);padding:4px 0 4px 14px;position:relative}
  .rc-row::before{content:"";position:absolute;left:2px;top:10px;width:6px;height:6px;border-radius:50%;background:var(--chip-ink);opacity:.55}
  .rc-row b{color:var(--text);font-weight:600}
  .rc-act{color:var(--accent);font-weight:600}
  /* 连续自适应台阶（A10）：全部由栏 min 宽度推导——
     展开三栏 200+320+360=880 起；图标轨 56+320+360=736 起；两栏 320+360=680 起；再窄堆叠 */
  .top .navbtn{display:none}
  .backdrop{display:none}
  #readbar{display:none;gap:10px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:var(--panel);z-index:2}
  @media (max-width:879px){
    .top{flex-wrap:wrap;row-gap:6px}
    .search{order:9;flex-basis:100%;max-width:none}
  }
  /* 图标轨（A10④）：736–879 侧栏收成 56px，首字符当图标，计数/快捷键说明收起 */
  @media (min-width:736px) and (max-width:879px){
    .app{grid-template-columns:56px minmax(320px,min(420px,26vw)) minmax(360px,1fr)}
    .write{margin:8px 6px;width:calc(100% - 12px);padding:8px 0;font-size:0}
    .write::before{content:"✎";font-size:16px}
    .side{padding:6px 6px 16px}
    .side .h{font-size:0;padding:12px 0 2px;text-align:center;letter-spacing:0}
    .side .h::first-letter{font-size:10px}
    .side .item{display:block;font-size:0;text-align:center;padding:8px 0}
    .side .item::first-letter{font-size:13px}
    .side .item .c{display:none}
    .hint{display:none}
  }
  /* 抽屉台阶（A10②）：≤735 侧栏转固定抽屉（☰ 开、背板/选中关），列表+详情两栏 */
  @media (max-width:735px){
    .top .navbtn{display:inline-block}
    .app{grid-template-columns:minmax(320px,min(420px,26vw)) minmax(360px,1fr)}
    .app .col:first-child{position:fixed;top:53px;bottom:0;left:0;width:264px;margin:0;z-index:7;
      transform:translateX(-105%);transition:transform .18s ease;background:var(--panel)}
    body.nav-open .app .col:first-child{transform:none;box-shadow:8px 0 24px rgba(29,32,38,.18)}
    body.nav-open .backdrop{display:block;position:fixed;top:53px;right:0;bottom:0;left:0;background:rgba(29,32,38,.28);z-index:6}
  }
  /* 堆叠台阶：≤679 放不下两栏 → 列表在上（限高内滚），详情在下（自然高，页面滚动） */
  @media (max-width:679px){
    .app{display:block;height:auto}
    .app .col{overflow:visible;border-right:none;border-bottom:1px solid var(--line)}
    .app .col:nth-child(2){max-height:46vh;overflow:auto}
    .app .col:last-child{border-bottom:none;min-height:50vh}
    .read{padding:14px 16px 60px}
    #readbar{display:flex}
  }
</style>
</head>
<body>
  <div class="top">
    <button class="ghost navbtn" id="navbtn" title="收起 / 展开导航">☰ 导航</button>
    <a href="/mail?token=" id="homelink"><img class="logo" src="/brand/lockup-h-light.svg" alt="agent-mailbox · 信箱"><img class="logo dark" src="/brand/lockup-h-dark.svg" alt="" aria-hidden="true"></a>
    <input class="search" id="search" placeholder="搜索全部往来（人 + agent）…（快捷键 /）">
    <span class="spacer"></span>
    <a class="dim aux" href="/setup" id="nav-setup">接入向导</a>
    <a class="dim aux" href="/visibility" id="nav-visibility">可见性</a>
    <button class="ghost" id="theme" title="切换浅色 / 深色" aria-label="切换浅色 / 深色"><svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" style="display:block;margin:auto"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor"/></svg></button>
    <span class="dim aux">＜ <b id="owner-name">boss</b>（主人 · 全可见）</span>
    <span class="me"><span class="av" id="owner-av">B</span></span>
  </div>
  <div class="backdrop" id="backdrop"></div>

  <div class="app">
    <!-- ============ 左栏 ============ -->
    <div class="col">
      <button class="primary write" id="btn-write">＋ 写一封信</button>
      <div class="side">
        <div class="h">我的</div>
        <div class="item on" data-folder="inbox">收件箱 <span class="c" id="c-inbox"></span></div>
        <div class="item" data-folder="starred">★ 星标 <span class="c" id="c-starred"></span></div>
        <div class="item" data-folder="sent">已发送 <span class="c" id="c-sent"></span></div>
        <div class="item" data-folder="drafts">草稿 <span class="c" id="c-drafts"></span></div>
        <div class="item" data-folder="archived">已归档 <span class="c" id="c-archived"></span></div>

        <div class="h">监控 · 只读</div>
        <div class="item" data-folder="monitor">agent 之间的全部往来 <span class="c" id="c-monitor"></span></div>
        <div class="item" data-folder="undelivered">未送达 / 失败 <span class="c" id="c-undelivered" style="color:var(--bad)"></span></div>
        <div class="item" data-folder="sealed">密封信（仅元数据） <span class="c" id="c-sealed"></span></div>

        <div class="h">成员</div>
        <div id="members"></div>
        <div class="item" id="btn-add-member">＋ 添加成员</div>

        <div class="h">其它</div>
        <div class="item" data-folder="health">通道体检</div>
        <div class="item" onclick="location.href='/?token='+TOKEN">任务卡看板</div>
        <div class="item" onclick="location.href='/visibility?token='+TOKEN">规则 / 可见性</div>
      </div>
      <div class="hint">快捷键：<b>j/k</b> 上下封 · <b>e</b> 归档 · <b>r</b> 回复 · <b>t</b> 派成任务卡 · <b>/</b> 搜索</div>
    </div>

    <!-- ============ 中栏：列表 ============ -->
    <div class="col">
      <div class="banner" id="banner"></div>
      <div class="bar">
        <span class="h" id="list-title">收件箱</span><span class="dim aux" id="list-sub"></span>
        <span class="spacer"></span>
        <button class="small ghost" id="btn-refresh">刷新</button>
      </div>
      <div id="list"></div>
      <div id="healthview" style="display:none;padding:12px"></div>
    </div>

    <!-- ============ 右栏：正文 ============ -->
    <div class="col">
      <div id="readbar"><button class="small ghost" id="backtolist">← 返回列表</button><span class="dim aux">信件详情</span></div>
      <div class="read" id="read"><div class="reader-empty" id="reader-empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg><div class="ttl">还没选信</div><div class="dim2">← 从中间选一封信，或直接写一封 —— 收发信件不需要任何模型。</div><button class="primary" id="empty-write">＋ 写一封信</button></div></div>
    </div>
  </div>

<script>
const token = new URLSearchParams(location.search).get("token") || localStorage.getItem("mb_token") || "";
localStorage.setItem("mb_token", token);
const TOKEN = token;
const hdr = { "Authorization": "Bearer " + token, "Content-Type": "application/json" };
document.getElementById("homelink").href = "/mail?token=" + token;

/* theme: same rules as the board page */
const rootEl = document.documentElement;
const qTheme = new URLSearchParams(location.search).get("theme");
rootEl.dataset.theme = qTheme || localStorage.getItem("mb_theme")
  || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
document.getElementById("theme").addEventListener("click", () => {
  const next = rootEl.dataset.theme === "dark" ? "light" : "dark";
  rootEl.dataset.theme = next; localStorage.setItem("mb_theme", next);
});

/* 自适应台阶（A10）：≤735 侧栏抽屉（☰ 开、背板/选中关）；≤679 堆叠时详情区给「← 返回列表」 */
const closeNav = () => document.body.classList.remove("nav-open");
document.getElementById("navbtn").addEventListener("click", () =>
  document.body.classList.toggle("nav-open"));
document.getElementById("backdrop").addEventListener("click", closeNav);
document.querySelector(".app .col:first-child").addEventListener("click", e => {
  if (e.target.closest(".item, .write")) closeNav();   // 选完即收，列表始终可见
});
document.getElementById("backtolist").addEventListener("click", () => {
  document.querySelector(".app .col:nth-child(2)").scrollIntoView({ behavior: "smooth", block: "start" });
});

function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast";
    t.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--bad-bg);color:var(--bad);border:1px solid var(--bad-line);border-radius:8px;padding:8px 16px;font-size:13px;display:none;max-width:80vw;z-index:9";
    document.body.appendChild(t); }
  t.textContent = msg; t.style.display = "block";
  setTimeout(() => t.style.display = "none", 4200);
}

function toastErr(msg) {  /* 错误不是裸报错字：结论 + 原因 + 下一步 成对 */
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast";
    t.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--bad-bg);color:var(--bad);border:1px solid var(--bad-line);border-radius:8px;padding:8px 16px;font-size:13px;display:none;max-width:80vw;z-index:9";
    document.body.appendChild(t); }
  t.innerHTML = '<b>操作没做成</b><div>' + esc(msg) + '</div>' +
    '<div style="opacity:.75;margin-top:2px">下一步：按上面的原因处理后重试；列表没变化就点「刷新」。</div>';
  t.style.display = "block";
  setTimeout(() => t.style.display = "none", 6000);
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
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML.replace(/"/g, "&quot;"); }

/* 时间统一本地时区显示 (§5⑪)：ISO-UTC → 浏览器本地时间 */
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = n => String(n).padStart(2, "0");
  if (sameDay) return hm(d.getHours()) + ":" + hm(d.getMinutes());
  if (d.getFullYear() === now.getFullYear())
    return (d.getMonth() + 1) + "-" + d.getDate() + " " + hm(d.getHours()) + ":" + hm(d.getMinutes());
  return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
}
function fmtFull(iso) {
  const d = new Date(iso); if (isNaN(d)) return String(iso || "");
  return d.toLocaleString();
}

let DATA = { letters: [], members: [], drafts: [], starred: [], counts: {}, attention_unread: 0 };
let folder = "inbox", sel = null, memberFilter = null, q = "";

function folderLetters() {
  const L = DATA.letters || [];
  const mine = x => x.to === DATA.owner;
  let out;
  if (folder === "inbox") out = L.filter(x => mine(x) && !x._archived);
  else if (folder === "archived") out = L.filter(x => mine(x) && x._archived);
  else if (folder === "sent") out = L.filter(x => x.from === DATA.owner);
  else if (folder === "starred") out = L.filter(x => (DATA.starred || []).includes(x.id));
  else if (folder === "monitor") out = L.filter(x => x.monitor);
  else if (folder === "undelivered") out = L.filter(x => x.undeliverable);
  else if (folder === "sealed") out = L.filter(x => x.redacted === "sealed");
  else out = L.filter(x => mine(x) && !x._archived);
  if (memberFilter) out = out.filter(x => x.from === memberFilter || x.to === memberFilter);
  if (q) {
    const s = q.toLowerCase();
    out = out.filter(x =>
      (x.subject || "").toLowerCase().includes(s) ||
      (x.from || "").toLowerCase().includes(s) || (x.to || "").toLowerCase().includes(s) ||
      (x.redacted !== "sealed" && (x.body || "").toLowerCase().includes(s)));
  }
  return out;
}

function tagHtml(x) {
  let t = "";
  if (x.to === DATA.owner && x.status === "pending" && x.attention === "decision") t += '<span class="tag need">需你拍板</span>';
  if (x.redacted === "sealed") t += '<span class="tag seal">密封</span>';
  if (x.undeliverable) t += '<span class="tag undeliver">未送达</span>';
  if (x.origin === "external") t += '<span class="tag ext">外部来源</span>';
  return t;
}
function fromLabel(x) {
  if (x.monitor) return esc(x.from) + " → " + esc(x.to);
  if (x.to === DATA.owner) return esc(x.from);
  return "→ " + esc(x.to);
}

/* 空状态行动点（§3.6）：0 封时给「给你的 agent 写第一封信」，不出现空白页 */
function emptyCtaHtml() {
  const opts = (DATA.members || []).filter(m => m.id !== DATA.owner)
    .map(m => '<option value="' + esc(m.id) + '">' + esc(m.id) + '</option>').join("");
  const pick = opts
    ? '<select id="cta-to">' + opts + '<option value="__other">其他…</option></select><input id="cta-to-other" placeholder="或输入成员 id" style="display:none">'
    : '<input id="cta-to" placeholder="收件人 id（如 ZC）">';
  return '<div class="empty-cta" id="empty-cta">' +
    '<div class="ttl">这里还没有信</div>' +
    '<div class="dim aux">给你的 agent 写第一封信 —— 选收件人 → 写一句 → 发。收发信件不需要任何模型。</div>' +
    '<form id="cta-form">' +
    '<div class="row">' + pick + '</div>' +
    '<input id="cta-subject" placeholder="主题（如：你好，这是你的信箱）">' +
    '<input id="cta-body" placeholder="写一句话…">' +
    '<div class="row"><button class="primary" type="submit">写第一封信并发送</button></div>' +
    '<div class="dim" style="font-size:11px">信＝往来与告知；任务卡＝要人动手的活。这封是信。</div>' +
    '</form></div>';
}

function renderList() {
  const list = document.getElementById("list");
  const hv = document.getElementById("healthview");
  if (folder === "health") { list.innerHTML = ""; hv.style.display = "block"; renderHealth(); return; }
  hv.style.display = "none";
  const titles = { inbox: "收件箱", starred: "星标", sent: "已发送", drafts: "草稿", archived: "已归档",
    monitor: "agent 之间的全部往来", undelivered: "未送达 / 失败", sealed: "密封信（仅元数据）" };
  document.getElementById("list-title").textContent = (memberFilter ? "成员 · " + memberFilter + " · " : "") + (titles[folder] || "信箱");
  if (folder === "drafts") return renderDrafts();
  const items = folderLetters();
  document.getElementById("list-sub").textContent = items.length ? items.length + " 封" : "";
  if (!items.length) { list.innerHTML = (folder === "inbox" && !memberFilter && !q) ? emptyCtaHtml() :
    '<div class="placeholder">这一栏没有信</div>'; bindCta(); return; }
  list.innerHTML = items.map(x => {
    const on = sel === x.id ? " on" : "";
    const unread = x.read ? "" : " unread";
    const snip = x.redacted === "sealed" ? "已密封 · 内容不进人类视图（仅元数据）"
      : (x.body || "").slice(0, 90).replace(/\\n/g, " ");
    return '<div class="listrow' + on + unread + '" data-id="' + esc(x.id) + '">' +
      '<span class="star' + ((DATA.starred || []).includes(x.id) ? " on" : "") + '" data-star="' + esc(x.id) + '">' +
        ((DATA.starred || []).includes(x.id) ? "★" : "☆") + '</span>' +
      '<span class="from">' + fromLabel(x) + '</span>' +
      '<div><div class="subj">' + esc(x.subject || "(无主题)") + '</div>' +
      '<div class="snip">' + esc(snip) + tagHtml(x) + '</div></div>' +
      '<span class="time">' + fmtTime(x.created_at) + '</span></div>';
  }).join("");
  list.querySelectorAll(".listrow").forEach(r => r.addEventListener("click", e => {
    if (e.target.dataset.star) return;
    openLetter(r.dataset.id);
  }));
  list.querySelectorAll("[data-star]").forEach(s => s.addEventListener("click", async e => {
    e.stopPropagation();
    const id = s.dataset.star, on = (DATA.starred || []).includes(id);
    try { await api("/api/mail/" + id + (on ? "/unstar" : "/star"), {}); await reload(false); }
    catch (err) { toastErr(err.message); }
  }));
}

function renderDrafts() {
  const ds = DATA.drafts || [];
  document.getElementById("list-sub").textContent = ds.length ? ds.length + " 封" : "";
  if (!ds.length) { document.getElementById("list").innerHTML = '<div class="placeholder">没有草稿 —— 点「＋ 写一封信」</div>'; return; }
  document.getElementById("list").innerHTML = ds.map(d =>
    '<div class="listrow" data-draft="' + esc(d.id) + '"><span class="star"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg></span>' +
    '<span class="from">草稿</span><div><div class="subj">' + esc(d.subject || "(无主题)") + '</div>' +
    '<div class="snip">收件人：' + esc(d.to || "（未填）") + '</div></div>' +
    '<span class="time">' + fmtTime(d.saved_at) + '</span></div>').join("");
  document.querySelectorAll("[data-draft]").forEach(r => r.addEventListener("click", () => {
    const d = (DATA.drafts || []).find(x => x.id === r.dataset.draft);
    if (d) compose(d);
  }));
}

async function renderHealth() {
  const hv = document.getElementById("healthview");
  hv.innerHTML = '<div class="dim">扫描中…</div>';
  let rep;
  try { rep = await api("/api/status"); } catch (err) { hv.innerHTML = '<div class="note">' + esc(err.message) + '</div>'; return; }
  const rows = (rep.members || []).map(m => {
    const chans = m.channels || [];
    const bad = chans.filter(c => c.status === "broken");
    const stat = !chans.length ? '<span class="dot idle"></span>未发现通道'
      : bad.length === chans.length ? '<span class="dot bad"></span>' + bad.length + ' 条通道全断'
      : bad.length ? '<span class="dot warn"></span>' + bad.length + ' 条断'
      : '<span class="dot ok"></span>通';
    return '<tr><td><b>' + esc(m.member) + '</b></td><td>' + stat + '</td>' +
      '<td class="dim">' + esc(bad.length ? bad[0].reason : (chans[0] && chans[0].reason) || "") + '</td></tr>';
  }).join("");
  hv.innerHTML = '<div class="bar" style="position:static;border:none;padding:0 0 8px"><span class="h">通道体检</span>' +
    '<span class="spacer"></span><button class="small" id="btn-rescan">重新扫描</button></div>' +
    (rows ? '<table><tr><th>成员</th><th>结果</th><th>说明</th></tr>' + rows + '</table>'
      : '<div class="dim">没有发现成员 —— 去 <a href="/setup?token=" id="hl-setup">接入向导</a> 扫一遍。</div>') +
    '<div class="hint" style="padding-left:0">断了的会在这里 + 收件箱被提醒（不再静默坏掉）。</div>';
  const rs = document.getElementById("btn-rescan");
  if (rs) rs.addEventListener("click", renderHealth);
  const hl = document.getElementById("hl-setup");
  if (hl) hl.href = "/setup?token=" + TOKEN;
}

function renderMembers() {
  const box = document.getElementById("members");
  box.innerHTML = (DATA.members || []).map(m => {
    const label = { ok: "通", bad: "收不到", warn: "可疑", idle: "未实测" }[m.dot] || "未实测";
    return '<div class="item" data-member="' + esc(m.id) + '">' +
      '<span class="dot ' + m.dot + '"></span>' + esc(m.id) +
      '<span class="c">' + label + '</span></div>';
  }).join("");
  box.querySelectorAll("[data-member]").forEach(el => el.addEventListener("click", () => {
    memberFilter = memberFilter === el.dataset.member ? null : el.dataset.member;
    renderList(); renderMembers();
  }));
}

function renderBanner() {
  const b = document.getElementById("banner");
  /* 打扰三档（§3.4）：默认只有「需你拍板」一档提醒；报备静默入箱；存档不提醒 */
  if (DATA.attention_unread > 0) {
    b.style.display = "block";
    b.innerHTML = "有 <b>" + DATA.attention_unread + "</b> 封信标着「需你拍板」等你处理（其余信件静默入箱，不打扰你）";
  } else b.style.display = "none";
}

function letterById(id) { return (DATA.letters || []).find(x => x.id === id); }

function openLetter(id) {
  sel = id;
  const x = letterById(id);
  if (!x) return;
  api("/api/mail/" + id + "/read", {}).then(reload.bind(null, false)).catch(() => {});
  renderList(); renderRead(x);
  /* 窄屏（上下结构）：详情在列表下方 —— 打开后滚到详情，返回按钮滚回列表 */
  if (matchMedia("(max-width:900px)").matches)
    document.getElementById("readbar").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRead(x) {
  const r = document.getElementById("read");
  const mon = x.monitor;
  const member = (DATA.members || []).find(m => m.id === x.from);
  const dot = member ? member.dot : "idle";
  const dotLabel = { ok: "正常", bad: "收不到回信", warn: "可疑", idle: "未实测" }[dot];
  const sealedNote = x.redacted === "sealed"
    ? '<div class="note warn"><b>密封信</b>：内容不进任何人类视图（谁 / 何时 / 几条之外，只有收件的 agent 能读）—— 防"全可见"变成泄密通道。</div>' : "";
  const needNote = (x.to === DATA.owner && x.status === "pending" && x.attention === "decision")
    ? '<div class="note ok"><b>这封信要你回一句</b> —— 它被发信方标了「需你拍板」，所以提醒你。</div>' : "";
  const extNote = x.origin === "external"
    ? '<div class="note"><b>外部来源信</b>：默认只显示、不触发任何动作（防 prompt injection 管道）。' +
      '<div style="margin-top:8px"><button class="small" id="btn-confirm-ext">我看过内容，确认执行</button></div></div>' : "";
  const arch = mon ? '<button class="small ghost" disabled title="agent 之间的信人是只读监看">归档</button>'
    : '<button class="small" id="btn-archive">归档</button>';
  const thread = (DATA.letters || [])
    .filter(t => x.thread_id && t.thread_id === x.thread_id)
    .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
  const threadHtml = thread.length > 1
    ? '<div class="thread">' + thread.map(t =>
        '<div class="b"><div class="who">' + esc(t.from) + ' → ' + esc(t.to) +
        '<span class="t">' + fmtFull(t.created_at) + '</span></div>' +
        '<div>' + (t.redacted === "sealed" ? "<i>（密封信 · 仅元数据）</i>" : esc((t.body || "").slice(0, 300))) + '</div></div>').join("") + '</div>'
    : "";

  /* 件五-D 回执链：handled_log 逐条（谁/何时/动作/时延），三态可区分；monitor 信不展示（权限面） */
  const wl = x.monitor ? [] : (x.handled_log || []);
  const t0 = Date.parse(x.created_at);
  const ACT = { wake: "唤醒", sampling: "采样唤醒", done: "已处理", intent: "开始处理",
    outcome: "处理完成", wake_alert: "唤醒失败告警", jev_skip: "低分跳过唤醒", reclaim: "过期重投" };
  const rcState = wl.some(e => e.action === "done") ? '<span class="rc-state done">已处理</span>'
    : wl.some(e => e.action === "wake" || e.action === "sampling") ? '<span class="rc-state waking">已唤醒 · 尚未处理</span>'
    : '<span class="rc-state">尚未唤醒</span>';
  const rcRows = wl.map(e => {
    let lat = "";
    const at = Date.parse(e.at);
    if (!isNaN(at) && !isNaN(t0)) {
      const sec = Math.max(0, Math.round((at - t0) / 1000));
      lat = sec < 1 ? "同秒" : sec < 60 ? "距来信 " + sec + " 秒" : "距来信 " + Math.floor(sec / 60) + " 分 " + (sec % 60) + " 秒";
    }
    return '<div class="rc-row"><b>' + esc(e.by || "?") + '</b>' +
      '<span class="rc-act">' + esc(ACT[e.action] || e.action) +
      ' <span style="opacity:.7;font-weight:400">' + esc(e.action) + '</span></span>' +
      '<span>' + esc(fmtFull(e.at)) + '</span>' + (lat ? '<span>' + esc(lat) + '</span>' : '') +
      (e.note ? '<span style="opacity:.75">' + esc(String(e.note)) + '</span>' : '') + '</div>';
  }).join("");
  const rcHtml = x.monitor ? "" :
    '<div class="rc"><div class="h">回执链 · handled_log' + rcState + '</div>' +
    (rcRows || '<div class="rc-row">这封信还没有任何回执 —— 唤醒后这里会出现 wake / done 时间线。</div>') + '</div>';
  r.innerHTML =
    '<div class="head"><span class="av">' + esc((x.from || "?").slice(0, 2).toUpperCase()) + '</span>' +
    '<div style="flex:1"><div><b>' + esc(x.from) + '</b> <span class="dim">→ ' + esc(x.to) + '</span>' + tagHtml(x) + '</div>' +
    '<div class="dim" style="font-size:12px">' + fmtFull(x.created_at) + '</div></div>' +
    '<span class="star' + ((DATA.starred || []).includes(x.id) ? " on" : "") + '" data-star="' + esc(x.id) + '" style="font-size:16px;cursor:pointer">' +
      ((DATA.starred || []).includes(x.id) ? "★" : "☆") + '</span></div>' +
    '<h2>' + esc(x.subject || "(无主题)") + '</h2>' +
    '<div class="acts">' +
    '<button class="primary small" id="btn-reply">回复</button>' +
    '<button class="small" id="btn-forward">转发</button>' +
    '<button class="small" id="btn-task" title="信＝往来与告知；任务卡＝要人动手的活（todo→doing→review→done，自动提醒承办人）">派成任务卡</button>' +
    arch + '<button class="small ghost" id="btn-unread">标为未读</button></div>' +
    '<div class="kv">信 vs 任务卡：信用来说话（读完为止）；任务卡要人动手、有状态流转、动了会自动叫醒承办人。</div>' +
    (x.redacted === "sealed" ? '<div class="body dim"><i>（已密封 · 内容不进人类视图）</i></div>'
      : '<div class="body">' + esc(x.body || "") + '</div>') +
    needNote + sealedNote + extNote + threadHtml + rcHtml +
    '<div class="note warn" style="margin-top:16px">发件人 <b>' + esc(x.from) + '</b> 的唤醒通道：<span class="dot ' + dot + '"></span>' + dotLabel +
    '。如果发件人<b>收不到回信</b>，回信前这里会先警告你 —— 免得白回。</div>' +
    '<div class="hint" style="padding:12px 0 0">人是<b>只读监看</b> agent 之间的信；要发话就「写一封信」或「派成任务卡」。</div>';
  const star = r.querySelector("[data-star]");
  if (star) star.addEventListener("click", async () => {
    const on = (DATA.starred || []).includes(x.id);
    try { await api("/api/mail/" + x.id + (on ? "/unstar" : "/star"), {}); await reload(false); }
    catch (err) { toastErr(err.message); }
  });
  const bind = (id2, fn) => { const el = document.getElementById(id2); if (el) el.addEventListener("click", fn); };
  bind("btn-reply", () => compose({ to: x.from, subject: "Re: " + (x.subject || "").replace(/^Re:\\s*/i, ""), reply_to: x.id, body: "" }));
  bind("btn-forward", () => compose({
    subject: "Fwd: " + (x.subject || ""),
    body: (x.redacted === "sealed" ? "（密封信，内容不转发）" : "---------- 转发 ----------\\n" + x.body) }));
  bind("btn-task", () => makeTask(x));
  bind("btn-archive", () => api("/api/mail/" + x.id + "/archive", {}).then(() => { sel = null; reload(); })
    .catch(err => toastErr(err.message)));
  bind("btn-unread", () => api("/api/mail/" + x.id + "/unread", {}).then(() => reload()).catch(err => toastErr(err.message)));
  bind("btn-confirm-ext", () => api("/api/mail/" + x.id + "/confirm-external", {})
    .then(() => { toast("已确认：这封信此后可触发动作（已留痕）"); reload(); })
    .catch(err => toastErr(err.message)));
}

async function makeTask(x) {
  const candidates = (DATA.members || []).map(m => m.id).filter(i => i !== DATA.owner);
  const who = x.monitor ? x.to : x.from;
  const assignee = promptBind(candidates, who);
  if (!assignee) return;
  try {
    const out = await api("/api/mail/" + x.id + "/task", { assignee });
    toast("已派成任务卡 " + out.task.id + " 给 " + assignee + "（承办人会收到提醒，看板可跟踪）");
  } catch (err) { toastErr(err.message); }
}
function promptBind(candidates, suggest) {
  const who = prompt("派给谁（任务卡承办人）？", suggest && suggest !== DATA.owner ? suggest : (candidates[0] || ""));
  return (who || "").trim();
}

function compose(prefill) {
  sel = null;
  const opts = (DATA.members || []).filter(m => m.id !== DATA.owner)
    .map(m => '<option value="' + esc(m.id) + '">' + esc(m.id) + '</option>').join("");
  const r = document.getElementById("read");
  const p = prefill || {};
  r.innerHTML = '<div class="compose"><h2 style="font-size:15px">' + (p.id ? "编辑草稿" : "写一封信") + '</h2>' +
    '<div class="kv">信 vs 任务卡：说话、告知、回话 → 用信；要人动手的活 → 派任务卡（在看板）。</div>' +
    '<form id="compose-form">' +
    '<div><label>收件人</label><input id="f-to" list="member-ids" value="' + esc(p.to || "") + '" placeholder="成员 id（如 ZC）">' +
    '<datalist id="member-ids">' + opts + '</datalist></div>' +
    '<div><label>主题</label><input id="f-subject" value="' + esc(p.subject || "") + '" placeholder="主题"></div>' +
    '<div><label>正文</label><textarea id="f-body" placeholder="写点什么…">' + esc(p.body || "") + '</textarea></div>' +
    '<div class="row"><div><label>打扰档位（写给对方的）</label><select id="f-attention">' +
    '<option value="decision">需你拍板（会提醒收件人）</option>' +
    '<option value="report">报备（静默入箱）</option>' +
    '<option value="archive">存档（不提醒）</option></select></div>' +
    '<div><label>密封</label><label style="display:flex;gap:6px;align-items:center;color:var(--text)"><input type="checkbox" id="f-sealed"> 密封信（内容只有收件 agent 能读）</label></div></div>' +
    '<div class="foot"><button class="primary" type="submit">发送</button>' +
    '<button type="button" id="f-draft">存草稿</button>' +
    '<button type="button" class="ghost" id="f-cancel">取消</button></div></form></div>';
  document.getElementById("f-attention").value = p.attention || "decision";
  document.getElementById("f-sealed").checked = !!p.sealed;
  const payload = () => ({
    to: document.getElementById("f-to").value.trim(),
    subject: document.getElementById("f-subject").value.trim(),
    body: document.getElementById("f-body").value,
    attention: document.getElementById("f-attention").value,
    sealed: document.getElementById("f-sealed").checked,
    reply_to: p.reply_to || "",
  });
  document.getElementById("compose-form").addEventListener("submit", async e => {
    e.preventDefault();
    const d = payload();
    try {
      await api("/api/mail/send", d);
      if (p.id) await api("/api/mail/drafts/delete", { id: p.id }).catch(() => {});
      toast("已发给 " + d.to); reload();
    } catch (err) { toastErr(err.message); }
  });
  document.getElementById("f-draft").addEventListener("click", async () => {
    const d = payload();
    try { await api("/api/mail/drafts", { ...d, id: p.id }); toast("草稿已存"); folder = "drafts"; reload(); }
    catch (err) { toastErr(err.message); }
  });
  document.getElementById("f-cancel").addEventListener("click", () => { reload(); });
}

function bindCta() {
  const form = document.getElementById("cta-form");
  if (!form) return;
  const toSel = document.getElementById("cta-to");
  if (toSel && toSel.tagName === "SELECT") toSel.addEventListener("change", () => {
    document.getElementById("cta-to-other").style.display = toSel.value === "__other" ? "block" : "none";
  });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    let to = toSel.value;
    if (to === "__other") to = document.getElementById("cta-to-other").value.trim();
    try {
      await api("/api/mail/send", {
        to, subject: document.getElementById("cta-subject").value.trim() || "(无主题)",
        body: document.getElementById("cta-body").value, attention: "decision" });
      toast("第一封信已发给 " + to); reload();
    } catch (err) { toastErr(err.message); }
  });
}

function renderSidebarCounts() {
  const c = DATA.counts || {};
  for (const k of ["inbox", "starred", "sent", "drafts", "archived", "monitor", "undelivered", "sealed"]) {
    const el = document.getElementById("c-" + k);
    if (el) el.textContent = c[k] || "";
  }
}

function renderFolderSel() {
  document.querySelectorAll("[data-folder]").forEach(el =>
    el.classList.toggle("on", el.dataset.folder === folder));
}
async function reload(rerenderRead = true) {
  try {
    DATA = await api("/api/mail");
    renderSidebarCounts(); renderMembers(); renderBanner(); renderFolderSel(); renderList();
    if (rerenderRead && sel) { const x = letterById(sel); if (x) renderRead(x); }
  } catch (err) { toast("加载失败：" + err.message); }
}

document.querySelectorAll("[data-folder]").forEach(el => el.addEventListener("click", () => {
  folder = el.dataset.folder; memberFilter = null; sel = null; renderFolderSel(); renderList();
}));
document.getElementById("btn-write").addEventListener("click", () => compose({}));
document.getElementById("btn-add-member").addEventListener("click", async () => {
  const id = (prompt("新成员 id（字母数字_-）:") || "").trim();
  if (!id) return;
  const kind = (prompt("类型：owner（人）/ agent / guest？（留空默认 agent）", "agent") || "agent").trim();
  try { await api("/api/members", { id, kind }); toast("成员 " + id + " 已加入名册"); reload(false); }
  catch (err) { toastErr(err.message); }
});
document.getElementById("btn-refresh").addEventListener("click", () => reload());
const searchEl = document.getElementById("search");
searchEl.addEventListener("input", () => { q = searchEl.value.trim(); renderList(); });

/* 快捷键：j/k 上下 · e 归档 · r 回复 · t 派成任务卡 · / 搜索 */
document.addEventListener("keydown", e => {
  const typing = e.target.matches("input,textarea,select") || e.target.isContentEditable;
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); searchEl.focus(); return; }
  const items = folderLetters();
  const idx = items.findIndex(x => x.id === sel);
  if (e.key === "j" || e.key === "k") {
    e.preventDefault();
    if (!items.length) return;
    const next = e.key === "j" ? Math.min(items.length - 1, idx + 1) : Math.max(0, idx - 1);
    openLetter(items[idx === -1 ? 0 : next].id);
  } else if (e.key === "e" && sel) {
    const x = letterById(sel);
    if (x && !x.monitor) api("/api/mail/" + sel + "/archive", {}).then(() => reload()).catch(err => toastErr(err.message));
    else toast("agent 之间的信是只读监看，不能由人归档");
  } else if (e.key === "r" && sel) { const x = letterById(sel); if (x) compose({ to: x.from, subject: "Re: " + (x.subject || "").replace(/^Re:\\s*/i, ""), reply_to: x.id, body: "" }); }
  else if (e.key === "t" && sel) { const x = letterById(sel); if (x) makeTask(x); }
});

document.getElementById("empty-write").addEventListener("click", () => compose({}));

reload();
setInterval(() => { if (document.visibilityState === "visible") reload(false).catch(() => {}); }, 5000);
</script>
</body>
</html>
"""

SETUP_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-mailbox · 接入向导</title>
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png">
<style>
  :root{
    --bg:#f4f5f7; --panel:#fff; --card:#fff; --line:#e3e6ec; --line-soft:#eceef2;
    --text:#1d2026; --dim:#6e7585; --accent:#4a6fa5; --accent-ink:#fff;
    --ok:#629c72; --warn:#b8904f; --bad:#b4524e; --bad-bg:#faf1f0; --bad-line:#e6cfcd;
    --ok-bg:#f1f7f2; --ok-line:#cfe3d3; --warn-bg:#fbf6ee; --warn-line:#ecdfc7;
    --chip:#eef0f4; --chip-ink:#5a6172; --shadow:0 2px 8px rgba(29,32,38,.08);
  }
  html[data-theme="dark"]{
    --bg:#101216; --panel:#171a20; --card:#1d2129; --line:#282d38; --line-soft:#21252e;
    --text:#e3e6ee; --dim:#8a91a4; --accent:#7195cd; --accent-ink:#101216;
    --ok:#7db389; --warn:#c7a266; --bad:#d4807c; --bad-bg:#2a2021; --bad-line:#4a3234;
    --ok-bg:#1a2029; --ok-line:#2e4436; --warn-bg:#221f18; --warn-line:#4a3f2a;
    --chip:#242935; --chip-ink:#98a0b2; --shadow:0 2px 8px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:13px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1000px;margin:0 auto;padding:0 24px 64px}
  header{display:flex;gap:8px 14px;align-items:center;padding:20px 0 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  header .logo{height:36px;vertical-align:middle}
  header .logo.dark{display:none}
  html[data-theme="dark"] header .logo:not(.dark){display:none}
  html[data-theme="dark"] header .logo.dark{display:inline}
  .dim{color:var(--dim);font-size:12px}
  .chip{background:var(--chip);color:var(--chip-ink);border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600}
  .spacer{flex:1}
  button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:6px;padding:8px 12px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
  button.small{padding:4px 8px;font-size:12px}
  button.ghost{background:transparent}
  button:disabled{opacity:.5;cursor:default}
  .steps{display:flex;gap:10px;align-items:center;margin:16px 0 0;flex-wrap:wrap}
  .step{display:flex;gap:9px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:4px 12px 4px 4px;font-weight:600;cursor:pointer}
  .step .n{width:20px;height:20px;border-radius:50%;background:var(--chip);color:var(--chip-ink);display:grid;place-items:center;font-size:11px}
  .step.on{border-color:var(--accent)} .step.on .n{background:var(--accent);color:var(--accent-ink)}
  .step.done{border-color:var(--ok)} .step.done .n{background:var(--ok);color:#fff}
  /* 骨架屏（件五-C）：加载中不再空白，脉冲不用渐变（anti-cheap） */
  .skel{height:12px;border-radius:6px;background:var(--chip);margin:10px 0;animation:skel 1.2s ease-in-out infinite}
  @keyframes skel{50%{opacity:.45}}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .arrow{color:var(--dim)}
  section{display:none;padding-top:18px} section.on{display:block}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:var(--shadow);margin-bottom:16px}
  .card h2{font-size:13px;font-weight:600;margin:0 0 4px}
  .hint{margin:0 0 14px}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line-soft);vertical-align:top}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:600}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:1px}
  .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)} .dot.warn{background:var(--warn)} .dot.idle{background:var(--dim)}
  .src{display:inline-block;background:var(--chip);color:var(--chip-ink);border-radius:4px;padding:1px 6px;font-size:10px;margin:2px 3px 0 0;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:top}
  .note{margin-top:10px;padding:10px 12px;border-radius:6px;background:var(--bad-bg);border:1px solid var(--bad-line);font-size:12px}
  .note.warn{background:var(--warn-bg);border-color:var(--warn-line)}
  .note.ok{background:var(--ok-bg);border-color:var(--ok-line)}
  code{background:var(--chip);border-radius:4px;padding:1px 5px;font-size:12px}
  details{margin-top:12px;border-top:1px solid var(--line-soft);padding-top:10px}
  summary{cursor:pointer;color:var(--dim);font-size:12px}
  .bar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 0}
  .bar .b{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 12px;font-size:12px}
  .foot{display:flex;gap:10px;align-items:center;padding-top:6px;flex-wrap:wrap}
  .prog{height:4px;background:var(--chip);border-radius:2px;overflow:hidden;margin:6px 0 0}
  .prog i{display:block;height:100%;background:var(--accent);width:33%;transition:width .2s}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 style="margin:0"><img class="logo" src="/brand/lockup-h-light.svg" alt="agent-mailbox · 接入向导"><img class="logo dark" src="/brand/lockup-h-dark.svg" alt="" aria-hidden="true"></h1>
    <span class="chip">接入向导</span>
    <span class="dim">本机 127.0.0.1 · 零依赖 · 探测只读</span>
    <span class="spacer"></span>
    <span class="dim">模型：不自动处理来信（可选，随时开）</span>
  </header>

  <div class="steps">
    <span class="step on" data-s="0"><span class="n">1</span>发现你的智能体</span><span class="arrow">→</span>
    <span class="step" data-s="1"><span class="n">2</span>测一下通不通</span><span class="arrow">→</span>
    <span class="step" data-s="2"><span class="n">3</span>完事</span>
  </div>
  <div class="prog"><i id="prog"></i></div>

  <!-- ===== 第 1 步：发现 ===== -->
  <section class="on" id="s1">
    <div class="card">
      <h2 id="s1-title">正在扫描本机…</h2>
      <p class="hint dim">探测四层：装了没有（PATH / 应用 / 配置目录）· 接了没有（MCP 配置 / 邮箱真名册）· <b>怎么叫醒它</b>（深链协议 / 监听端口 / 可用命令）· 实测通不通（下一步）。探测只读，不动任何人的配置。</p>
      <div id="s1-body"></div>
      <div class="foot" style="margin-top:14px">
        <button class="ghost" id="btn-rescan">重新扫描</button>
        <span class="spacer"></span>
        <button class="primary" id="btn-to2">下一步：测一下</button>
      </div>
    </div>
  </section>

  <!-- ===== 第 2 步：测一下 ===== -->
  <section id="s2">
    <div class="card">
      <h2>发一封测试信，看谁真的收到</h2>
      <p class="hint dim">会给每个成员各发一封测试信，等回执（默认 15 秒）。不碰你的真实信件。每行都给<b>结果 + 下一步</b>——断了也有路可走，不只报丧。</p>
      <div id="s2-body"></div>
      <div class="note warn">测试信成功 ≠ 以后都行：应用更新、登录过期都会再断。<b>通道体检</b>（信箱左栏）会持续盯着，断了会提醒你 —— 不再出现"静默坏了没人知道"。</div>
      <div class="foot" style="margin-top:14px"><button class="ghost" id="btn-test-all">全部测一遍</button><span class="spacer"></span><button class="primary" id="btn-to3">下一步：完事</button></div>
    </div>
  </section>

  <!-- ===== 第 3 步：完事 ===== -->
  <section id="s3">
    <div class="card">
      <h2>装好了</h2>
      <p class="hint dim">后台收信服务会盯着信箱并在有新信时叫醒对应智能体（可选安装）。</p>
      <div id="s3-body" class="bar"></div>
      <div class="note ok" style="margin-top:14px">现在就给某个智能体写第一封信吧 —— 收发信件本身<b>不需要任何模型</b>。模型只是可选的"要不要邮箱自动替你处理来信"。</div>
      <div class="foot" style="margin-top:14px">
        <button class="primary" id="btn-open-mailbox">打开信箱</button>
        <button class="ghost" id="btn-back-fix">去修待修项</button>
        <span class="spacer"></span><span class="dim">高级：模型 / 体检 / 服务详情在信箱左栏</span>
      </div>
    </div>
  </section>
</div>

<script>
const token = new URLSearchParams(location.search).get("token") || localStorage.getItem("mb_token") || "";
localStorage.setItem("mb_token", token);
const hdr = { "Authorization": "Bearer " + token, "Content-Type": "application/json" };
const TOKEN = token;

const rootEl = document.documentElement;
rootEl.dataset.theme = new URLSearchParams(location.search).get("theme") || localStorage.getItem("mb_theme")
  || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML.replace(/"/g, "&quot;"); }
async function api(path, body) {
  const res = await fetch(path, body === undefined
    ? { headers: hdr } : { method: "POST", headers: hdr, body: JSON.stringify(body) });
  if (!res.ok) { let d2 = res.status + " " + res.statusText; try { d2 = (await res.json()).error || d2; } catch {} throw new Error(d2); }
  return res.json();
}

const steps = [...document.querySelectorAll(".step")];
const secs = [document.getElementById("s1"), document.getElementById("s2"), document.getElementById("s3")];
function go(i) {
  steps.forEach((s, j) => {
    s.classList.toggle("on", j === i);
    s.classList.toggle("done", j < i);          // 已完步骤打勾（✓ 字符，非 emoji）
    s.querySelector(".n").textContent = j < i ? "✓" : String(j + 1);
  });
  secs.forEach((s, j) => s.classList.toggle("on", j === i));
  document.getElementById("prog").style.width = ((i + 1) / 3 * 100) + "%";
  window.scrollTo(0, 0);
  if (i === 2) loadSummary();
}
steps.forEach(s => s.onclick = () => go(+s.dataset.s));

let REPORT = null;
const KIND_LABEL = { cli: "CLI", app: "App", config: "仅配置痕迹", unknown: "未识别" };

function chRow(ch) {
  const mark = { ok: '<span class="dot ok"></span>', broken: '<span class="dot bad"></span>', unknown: '<span class="dot idle"></span>' }[ch.status] || '<span class="dot idle"></span>';
  return '<div>' + mark + esc(ch.type) + ' <code>' + esc(ch.value || "") + '</code> ' +
    (ch.status === "broken" ? '<span class="dim">— ' + esc(ch.reason || "") + '</span>' : '') + '<br>' +
    '<span class="src" title="' + esc(ch.source || "") + '">来源: ' + esc(ch.source || "-") + '</span></div>';
}

async function scan() {
  const btn = document.getElementById("btn-rescan");
  btn.disabled = true;
  document.getElementById("s1-title").textContent = "正在扫描本机…（探测只读，不动任何配置）";
  document.getElementById("s1-body").innerHTML =
    '<div class="skel" style="width:62%"></div><div class="skel" style="width:85%"></div>' +
    '<div class="skel" style="width:74%"></div><div class="skel" style="width:90%"></div>' +
    '<div class="skel" style="width:45%"></div>';
  try { REPORT = await api("/api/discover", {}); }
  catch (err) {
    document.getElementById("s1-title").textContent = "扫描失败";
    document.getElementById("s1-body").innerHTML = '<div class="note">' + esc(err.message) + '</div>';
    btn.disabled = false; return;
  }
  btn.disabled = false;
  const ms = REPORT.members || [];
  document.getElementById("s1-title").innerHTML = ms.length
    ? '已自动发现 <b>' + ms.length + '</b> 个智能体 · 全部来自机器实测，无需你填任何东西'
    : "本机没发现受支持的智能体";
  if (!ms.length) {
    document.getElementById("s1-body").innerHTML =
      '<div class="note warn">一个都没找到？支持清单：<code>' + esc((REPORT.supported || []).join(" ")) + '</code><br>' +
      '装好其中任意一个后点「重新扫描」；自定义安装路径可先在命令行 <code>agent-mailbox discover --deep 目录</code> 深扫，或手动添加。</div>';
    return;
  }
  const rows = ms.map(m => {
    const conn = m.connected ? "已接入" : "装了未接";
    const chans = (m.channels || []).map(chRow).join("") || '<span class="dim">未发现唤醒通道（信只能等人主动取）</span>';
    const tested = (m.channels || []).map(c => c.status);
    const stat = tested.includes("broken") ? '<span class="dot bad"></span>有通道断'
      : tested.includes("ok") ? '<span class="dot ok"></span>通道在位'
      : '<span class="dot idle"></span>未实测';
    const stale = m.stale ? '<div class="dim" style="margin-top:4px">⚠ ' + esc(m.stale_note || "发现指纹已变化") + '</div>' : "";
    return '<tr><td><b>' + esc(m.member) + '</b><div class="dim">' + conn + '</div></td>' +
      '<td>' + esc(KIND_LABEL[m.kind] || m.kind || "-") + '</td>' +
      '<td>' + chans + stale + '</td><td>' + stat + '</td></tr>';
  }).join("");
  document.getElementById("s1-body").innerHTML =
    '<table><tr><th style="width:20%">智能体</th><th style="width:11%">形态</th><th>发现的唤醒通道</th><th style="width:15%">实测</th></tr>' + rows + '</table>' +
    '<details><summary>探测明细（我到底扫了哪些地方）</summary><div class="dim" style="margin-top:8px">PATH / 应用（Info.plist 深链）/ 配置目录 / MCP 配置 / 监听端口（按可执行路径反查，不靠进程名）。每条通道都标了来源，可核对。</div></details>';
}
document.getElementById("btn-rescan").addEventListener("click", scan);
document.getElementById("btn-to2").addEventListener("click", () => { go(1); renderTestRows(); });

/* ===== 第 2 步：测一下（结果 + 下一步成对出现） ===== */
let TEST_TARGETS = [];
function renderTestRows() {
  TEST_TARGETS = ((REPORT && REPORT.members) || []).map(m => m.member);
  const body = document.getElementById("s2-body");
  if (!TEST_TARGETS.length) {
    body.innerHTML = '<div class="note warn">没有可测的成员 —— 先回第 1 步扫描。</div>';
    return;
  }
  body.innerHTML = '<table><tr><th style="width:22%">智能体</th><th>结果</th><th style="width:38%">下一步</th></tr>' +
    TEST_TARGETS.map(mm =>
      '<tr id="tr-' + esc(mm) + '"><td><b>' + esc(mm) + '</b></td>' +
      '<td class="dim" data-cell="result">未测 <button class="small" data-test="' + esc(mm) + '">测一下</button></td>' +
      '<td class="dim" data-cell="next">—</td></tr>').join("") + '</table>';
  body.querySelectorAll("[data-test]").forEach(b => b.addEventListener("click", () => runTest(b.dataset.test)));
}

async function runTest(member) {
  const tr = document.getElementById("tr-" + member);
  if (!tr) return;
  const rc = tr.querySelector('[data-cell="result"]'), nc = tr.querySelector('[data-cell="next"]');
  rc.innerHTML = '<span class="dim">测试信发送中…（等回执）</span>';
  try {
    const r = await api("/api/test", { member, timeout: 15 });
    if (r.ok) {
      rc.innerHTML = '<span class="dot ok"></span>' + esc(r.reason) + ' <span class="dim">（' + esc(r.elapsed_s) + 's）</span>';
      nc.innerHTML = '<span class="dim">' + esc(r.next_step || "无需处理") + '</span>';
    } else {
      rc.innerHTML = '<span class="dot bad"></span><b>❌ 没等到回执</b> <span class="dim">（' + esc(r.elapsed_s) + 's）</span>';
      nc.innerHTML = esc(r.next_step || "") + ' <button class="small" data-test="' + esc(member) + '">修完再测</button>';
      nc.querySelector("[data-test]").addEventListener("click", () => runTest(member));
    }
  } catch (err) {
    rc.innerHTML = '<span class="dot bad"></span>❌ ' + esc(err.message);
    nc.innerHTML = '检查服务是否在跑（<code>agent-mailbox --web</code>），然后重试';
  }
}
document.getElementById("btn-test-all").addEventListener("click", async () => {
  for (const mm of TEST_TARGETS) await runTest(mm);
});
document.getElementById("btn-to3").addEventListener("click", () => go(2));

/* ===== 第 3 步：完事 ===== */
async function loadSummary() {
  const box = document.getElementById("s3-body");
  box.innerHTML = '<div class="skel" style="width:70%"></div><div class="skel" style="width:55%"></div><div class="skel" style="width:64%"></div>';
  let s;
  try { s = await api("/api/setup-summary"); }
  catch (err) { box.innerHTML = '<div class="b">加载失败：' + esc(err.message) + '</div>'; return; }
  const wake = s.wake && s.wake.configured
    ? '<div class="b"><span class="dot ok"></span>后台收信服务 <b>已开启</b> <span class="dim">（' + esc(s.wake.agent_id || "") + '）</span></div>'
    : '<div class="b"><span class="dot idle"></span>后台收信服务 <b>未安装</b> <span class="dim">（可选：python -m agent_mailbox.wake install）</span></div>';
  const brokenN = (s.broken || []).length;
  const fix = brokenN
    ? '<div class="b"><span class="dot bad"></span>待修 <b>' + brokenN + '</b> <span class="dim">（' + esc((s.broken || []).map(x => x.member).join("、")) + '）</span></div>'
    : '<div class="b"><span class="dot ok"></span>待修 <b>0</b></div>';
  box.innerHTML =
    wake +
    '<div class="b">接入智能体 <b>' + s.registered + ' / ' + s.discovered + '</b></div>' +
    fix +
    '<div class="b">模型 <b>不自动处理来信</b> <span class="dim">（可选，随时开）</span></div>';
}
document.getElementById("btn-open-mailbox").addEventListener("click", () => { location.href = "/mail?token=" + TOKEN; });
document.getElementById("btn-back-fix").addEventListener("click", () => go(1));

scan();
</script>
</body>
</html>
"""

VISIBILITY_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-mailbox · 可见性</title>
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png">
<style>
  :root{
    --bg:#f4f5f7; --panel:#fff; --card:#fff; --line:#e3e6ec; --line-soft:#eceef2;
    --text:#1d2026; --dim:#6e7585; --accent:#4a6fa5; --accent-ink:#fff;
    --bad:#b4524e; --bad-bg:#faf1f0; --bad-line:#e6cfcd;
    --ok-bg:#f1f7f2; --ok-line:#cfe3d3;
    --chip:#eef0f4; --chip-ink:#5a6172; --shadow:0 2px 8px rgba(29,32,38,.08);
  }
  html[data-theme="dark"]{
    --bg:#101216; --panel:#171a20; --card:#1d2129; --line:#282d38; --line-soft:#21252e;
    --text:#e3e6ee; --dim:#8a91a4; --accent:#7195cd; --accent-ink:#101216;
    --bad:#d4807c; --bad-bg:#2a2021; --bad-line:#4a3234;
    --ok-bg:#1a2029; --ok-line:#2e4436;
    --chip:#242935; --chip-ink:#98a0b2; --shadow:0 2px 8px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:13px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
  .wrap{max-width:860px;margin:0 auto;padding:0 24px 64px}
  header{display:flex;gap:8px 14px;align-items:center;padding:18px 0 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  header .logo{height:34px;vertical-align:middle}
  header .logo.dark{display:none}
  html[data-theme="dark"] header .logo:not(.dark){display:none}
  html[data-theme="dark"] header .logo.dark{display:inline}
  .dim{color:var(--dim);font-size:12px}
  .chip{background:var(--chip);color:var(--chip-ink);border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600}
  .spacer{flex:1}
  button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:6px;padding:8px 12px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:var(--shadow);margin-bottom:16px}
  .card h2{font-size:13px;font-weight:600;margin:0 0 8px}
  .hint{margin:0 0 12px}
  .row{display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  .row label.lb{width:170px;flex:none;color:var(--dim)}
  .sw{width:34px;height:19px;border-radius:999px;background:var(--accent);position:relative;flex:none;cursor:pointer;border:none;padding:0}
  .sw i{position:absolute;top:2px;right:2px;width:15px;height:15px;border-radius:50%;background:#fff}
  .sw.off{background:var(--chip)} .sw.off i{right:auto;left:2px}
  .sw[disabled]{opacity:.45;cursor:not-allowed}
  .note{margin-top:12px;padding:10px 12px;border-radius:6px;background:var(--bad-bg);border:1px solid var(--bad-line);font-size:12px}
  .note.ok{background:var(--ok-bg);border-color:var(--ok-line)}
  code{background:var(--chip);border-radius:4px;padding:1px 5px;font-size:12px}
  .foot{display:flex;gap:10px;align-items:center;padding-top:8px;flex-wrap:wrap}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 16px;font-size:13px;display:none;max-width:80vw;box-shadow:var(--shadow)}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line-soft);font-size:12px}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 style="margin:0"><img class="logo" src="/brand/lockup-h-light.svg" alt="agent-mailbox · 可见性"><img class="logo dark" src="/brand/lockup-h-dark.svg" alt="" aria-hidden="true"></h1>
    <span class="chip">可见性</span>
    <span class="dim">"真邮箱"的规矩：主人能看全店，agent 只能看自己的格子</span>
    <span class="spacer"></span>
    <button onclick="location.href='/mail?token='+TOKEN">← 回信箱</button>
  </header>

  <div class="card">
    <h2>谁能看到谁的信</h2>
    <p class="hint dim">四组开关 + 发信权限。默认值就是 §3.3 的权限模型 —— 改动会留痕（谁在什么时候改的）。</p>
    <div class="row"><label class="lb">主人（你）</label>
      <button class="sw" id="sw-owner_sees_all" aria-label="主人可见全部往来"><i></i></button>
      <span>可见<b>全部往来</b>（你的收件箱 + agent 之间的信）</span></div>
    <div class="row"><label class="lb">agent 之间互看</label>
      <button class="sw off" id="sw-agent_cross_read" aria-label="agent 之间互看"><i></i></button>
      <span class="dim">关 —— 每个 agent 只能看自己的收件箱（默认关，开了就是"公开抄送"，且有留痕）</span></div>
    <div class="row"><label class="lb">密封信进人类视图</label>
      <button class="sw off" id="sw-sealed_in_human_view" disabled aria-label="密封信进人类视图（锁定为关）"><i></i></button>
      <span class="dim"><b>锁定为关</b> —— 密封信只留元数据（谁/何时/几条），内容不进任何人类视图（§3.3 硬规则，防"全可见"变泄密通道）</span></div>
    <div class="row"><label class="lb">外部来源信自动执行</label>
      <button class="sw off" id="sw-external_auto_execute" aria-label="外部来源信自动执行"><i></i></button>
      <span class="dim">关 —— 非本机来源的信默认只显示、不触发动作，先给你看；要执行须你确认（防 prompt injection 管道）</span></div>
    <div class="row"><label class="lb">发信权限</label>
      <span class="dim">本机成员（人 + 已注册 agent）；<b>跨设备发信</b>需配对令牌（令牌可撤销、可轮换；0.7.6 提供配对流程）</span></div>
    <div class="note"><b>最要紧的一条</b>：agent 收到的信里如果有"去执行某件事"的要求（尤其来自外部/陌生来源），默认<b>不自动执行</b>，先变成"待你确认"。信箱不能成为一条"谁都能往你机器里塞指令"的管道。</div>
    <div class="foot"><span class="dim">改动会记录在案（谁在什么时候改了可见性）—— 可见性本身也要留痕。最近改动：</span></div>
    <div id="audit"><div class="dim">加载中…</div></div>
  </div>
</div>
<div id="toast"></div>
<script>
const token = new URLSearchParams(location.search).get("token") || localStorage.getItem("mb_token") || "";
localStorage.setItem("mb_token", token);
const TOKEN = token;
const hdr = { "Authorization": "Bearer " + token, "Content-Type": "application/json" };
const rootEl = document.documentElement;
rootEl.dataset.theme = new URLSearchParams(location.search).get("theme") || localStorage.getItem("mb_theme")
  || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML.replace(/"/g, "&quot;"); }
function toast(msg) { const t = document.getElementById("toast"); t.textContent = msg; t.style.display = "block"; setTimeout(() => t.style.display = "none", 4000); }
async function api(path, body) {
  const res = await fetch(path, body === undefined ? { headers: hdr } : { method: "POST", headers: hdr, body: JSON.stringify(body) });
  if (!res.ok) { let d2 = res.status + " " + res.statusText; try { d2 = (await res.json()).error || d2; } catch {} throw new Error(d2); }
  return res.json();
}
const NAMES = {
  owner_sees_all: "主人全可见", agent_cross_read: "agent 之间互看",
  sealed_in_human_view: "密封信进人类视图", external_auto_execute: "外部来源信自动执行",
};
function renderSw(key, on, locked) {
  const el = document.getElementById("sw-" + key);
  if (!el) return;
  el.classList.toggle("off", !on);
  el.disabled = !!locked;
}
function renderAudit(entries) {
  const box = document.getElementById("audit");
  if (!entries || !entries.length) { box.innerHTML = '<div class="dim">还没有改动记录（默认值＝权限模型本身）。</div>'; return; }
  box.innerHTML = '<table><tr><th>时间（本地）</th><th>谁</th><th>改了什么</th></tr>' + entries.map(e2 =>
    '<tr><td>' + esc(fmtLocal(e2.at)) + '</td><td>' + esc(e2.by || "-") + '</td><td>' +
    Object.entries(e2.changes || {}).map(([k, v]) => esc(NAMES[k] || k) + " → " + (v ? "开" : "关")).join("；") +
    '</td></tr>').join("") + '</table>';
}
function fmtLocal(iso) { const d = new Date(iso); return isNaN(d) ? String(iso || "") : d.toLocaleString(); }
async function load() {
  try {
    const v = await api("/api/visibility");
    for (const k of Object.keys(NAMES)) renderSw(k, v.visibility[k], (v.hard_locked || []).includes(k));
    renderAudit(v.audit);
  } catch (err) { toast("加载失败：" + err.message); }
}
for (const key of Object.keys(NAMES)) {
  document.getElementById("sw-" + key).addEventListener("click", async () => {
    const el = document.getElementById("sw-" + key);
    const next = el.classList.contains("off");
    try {
      const out = await api("/api/visibility", { [key]: next });
      for (const k of Object.keys(NAMES)) renderSw(k, out.visibility[k], (out.hard_locked || []).includes(k));
      renderAudit(out.audit);
      toast((NAMES[key]) + " 已" + (next ? "开启" : "关闭") + "（已留痕）");
    } catch (err) { toastErr(err.message); }
  });
}
load();
</script>
</body>
</html>
"""
