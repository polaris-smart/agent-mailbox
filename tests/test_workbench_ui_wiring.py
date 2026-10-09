"""UI 接线冒烟：菜单项、渲染器、授权按钮必须与代码一致。

老板指出「左侧菜单逻辑牵扯整个 APP UI 逻辑」——实测确实如此：一屏三问的交接台
有能力、有数据、有 CLI，却**没有菜单入口**（手工补上）。这类"加了视图忘接线"
的错靠人眼只能偶然发现，所以在这里用结构性断言钉住：
* 每个 `navLabels` 键都要有侧栏按钮（否则视图进不去）；
* 每个视图都要有渲染器（否则点进去空白）；
* 交接台必须在菜单里、且是选中项目后的默认视图；
* 授权面板必须提供「本任务内允许」（否则服务端的 allow_run 白做）。"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

import pathlib
import re

ASSETS = pathlib.Path(__file__).resolve().parents[1] / "src/agent_mailbox/workbench_assets"


def _label_keys(block: str) -> set[str]:
    """``navLabels`` 的键后面一定跟 t(...)；一行可能多个键，不能按行切。"""
    return set(re.findall(r"([a-z_]+):\s*t\(", block))


def _render_keys(block: str) -> set[str]:
    """``renders`` 的键后面跟函数名（单行映射）。"""
    return set(re.findall(r"([a-z_]+):\s*\w+", block))


def _js() -> str:
    return (ASSETS / "workbench.js").read_text(encoding="utf-8")


def _nav_labels() -> set[str]:
    js = _js()
    block = re.search(r"const navLabels = \{(.*?)\n\};", js, re.DOTALL).group(1)
    return _label_keys(block)


def test_every_view_has_a_sidebar_button():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    buttons = set(re.findall(r'data-view="([a-z_]+)"', html))
    missing = _nav_labels() - buttons
    assert missing == set(), f"这些视图没有菜单入口：{sorted(missing)}"


def test_every_view_has_a_renderer():
    js = _js()
    block = re.search(r"const renders = \{(.*?)\};", js, re.DOTALL).group(1)
    renders = _render_keys(block)
    assert _nav_labels() - renders == set(), (
        f"这些视图没有渲染器：{sorted(_nav_labels() - renders)}"
    )


def test_team_wall_is_reachable_and_default():
    js = _js()
    assert "wall" in _nav_labels(), "交接台不在菜单里（铁律 1 无处落地）"
    assert "function renderWall()" in js
    # 默认视图是条件式：有活的项目落交接台，空项目落概览（首跑引导）
    assert '? "wall" : "overview"' in js, "有活的项目应默认落在交接台"
    assert js.count('state.view = "overview";') >= 1, "新建的空项目应落在概览"


def test_approval_panel_offers_allow_for_task():
    js = _js()
    assert "allow_run" in js, "授权面板没有「本任务内允许」（服务端 capability 白做）"
    assert "本任务内允许" in js


def test_task_badge_counts_open_tasks():
    js = _js()
    assert "task-nav-count" in js
    assert 'task.status !== "done" && task.status !== "cancelled"' in js


def test_footer_links_to_the_public_repository():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'href="https://github.com/polaris-smart/agent-mailbox"' in html
    assert "GitHub（未发布）" not in html


# ── 以下为第 10 轮 ④-1..④-4 指出的"近乎失效"断言的强化版：改成结构性质检，不钉字面 ──


def _html_without_comments() -> str:
    """把 HTML 注释剥掉再判：否则把真按钮改成 `<!-- … -->` 也能过（R10 ④-4）。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def test_sidebar_buttons_are_real_markup_not_comments():
    html = _html_without_comments()
    buttons = set(re.findall(r'data-view="([a-z_]+)"', html))
    assert _nav_labels() - buttons == set(), (
        f"这些视图没有真按钮：{sorted(_nav_labels() - buttons)}"
    )


def test_every_renderer_has_a_body_not_a_stub():
    """只核键名不够：把 wall 映射到 noopRenderer 也能过（R10 ④-3）。"""
    js = _js()
    for view in sorted(_nav_labels()):
        match = re.search(
            rf"function render{view.capitalize()}\(\)\s*\{{(.{{0,200}})", js, re.DOTALL
        )
        assert match, f"{view} 没有渲染器函数"
        assert len(match.group(1).strip()) > 20, f"{view} 的渲染器是空壳"


def test_wall_answers_what_needs_me_first():
    """一屏三问的顺序：先答「该我干什么」，且授权与验收**拆组**（R10 + 两路评审）。

    用整文件相对位置断言，不解析函数体（正则在嵌套大括号上很脆）。
    """
    js = _js()
    marks = ["等你授权", "等你验收", "在跑", "卡住"]
    positions = [js.index(mark) for mark in marks]
    assert positions == sorted(positions), f"交接台分组顺序不对：{marks}"
    assert "pending_permissions" in js, "没消费待授权数据（FL-a）"
    assert "openTaskDetail(" in js, "交接台的行没有直达任务详情（可操作处）"


def test_wall_and_badge_announce_changes():
    """A1：计数徽标与交接台计数必须在 aria-live 区域内/自带 aria-live。"""
    html = _html_without_comments()
    assert 'id="task-nav-count" aria-live="polite"' in html
    assert '"aria-live": "polite"' in _js()


def test_approval_panel_guards_and_conditions_are_wired():
    """R10 ④-1/④-2：按钮显示条件与过期保护必须是真逻辑。"""
    js = _js()
    assert "runOffered" in js and "permission.options" in js, "授权面板没按 options 决定是否显示"
    assert "hasExpired()" in js, "授权面板丢了过期保护"


def test_error_codes_have_actionable_mappings():
    """铁律 2（A6）：这三条此前零映射，中文界面会漏英文原文。"""
    js = _js()
    for code in ("MAILBOX_TOOL_DENIED", "CONTACT_REQUIRED", "DELIVERY_CAPTURE_FAILED"):
        assert f"{code}:" in js, f"{code} 没有中文映射"


def test_confirmed_actions_and_proof_marker_are_wired():
    """A5 可逆性 + 证据前移：破坏性动作必须有确认；任务行必须能显示证明。"""
    js = _js()
    assert js.count("window.confirm(") >= 2, "撤销邮箱会话/移出项目缺少二次确认"
    assert "task.proof" in js, "任务行没有证据标记（证据没前移到决策点）"


def test_missing_proof_is_shown_explicitly():
    """诚实性：待验收/已验收的任务若没有证明，必须显示「证明=无」，不能静默。

    第 10 轮 + 第二轮评审（ux-ia2）指出：`delivery_proof` 只由 managed 执行发出，
    mailbox 任务行**永远不显示证明**，而缺省静默会让用户把"没标记"读成"没问题"。
    """
    js = _js()
    assert "证明=无" in js, "缺证明时必须显式显示「证明=无」"
    assert '["review", "done"].includes(task.status)' in js, "只在该有证明的状态上提示"


def test_confirm_never_runs_in_the_render_path():
    """渲染路径**不得**弹确认框 ✓（真实 bug ✗：`window.confirm` 曾在 map 循环里 ⇒ 死按钮 ✓）。

    判定：每个 `window.confirm(` 之前 240 字符内**必须**能找到事件绑定（addEventListener ✓），
    否则说明它跑在"构建 DOM"的路径上 ✗。
    """
    source = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    offenders = []
    for match in re.finditer(r"window\.confirm\(", source):
        head = source[max(0, match.start() - 240) : match.start()]
        if "addEventListener" not in head:
            line = source[: match.start()].count("\n") + 1
            offenders.append(line)
    assert offenders == [], f"这些 window.confirm 没绑在事件里（会在渲染时弹框 ✗）：行 {offenders}"


def test_every_nav_view_has_a_renderer_and_a_button():
    """三处齐不变量（本轮把「教室」接上后新增 ✓）：每个 `navLabels` 键都必须同时具备
    ① `renders` 映射条目 ✓ ② 侧栏 `data-view` 按钮 ✓ —— 少一处就是"加了视图忘接线" ✗。"""
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    html = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "index.html").read_text(
        encoding="utf-8"
    )
    nav_block = re.search(r"const navLabels = \{(.*?)\};", js, re.DOTALL)
    renders_block = re.search(r"const renders = \{(.*?)\};", js, re.DOTALL)
    assert nav_block and renders_block, "找不到 navLabels / renders 定义"
    nav_keys = set(re.findall(r"(\w+):\s*t\(", nav_block.group(1)))
    render_keys = set(re.findall(r"(\w+):\s*render\w+", renders_block.group(1)))
    button_keys = set(re.findall(r'data-view="([a-z]+)"', html))
    assert nav_keys == render_keys, f"侧栏键与渲染映射不一致 ✗：{nav_keys ^ render_keys}"
    missing = nav_keys - button_keys
    assert not missing, f"这些视图没有侧栏按钮（进不去 ✗）：{missing}"
    assert "classroom" in nav_keys, "教室视图必须已接线 ✓"


def test_blackboard_shows_the_four_verbs_and_proof():
    """任务黑板必须体现**四动词**（派→接→交→验）与**证据**标记 ✓（老板要的"流程+证据"✓）。"""
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "function verbStage(" in js, "缺四动词映射 ✓"
    for verb in (
        't("派", "Dispatch")',
        't("接", "Accept")',
        't("交", "Submit")',
        't("验", "Verify")',
    ):
        assert verb in js, f"四动词缺 {verb} ✗"
    assert "task.proof" in js, "黑板必须显示证明有无 ✓（task.proof 来自 proof_index ✓）"
    assert "blackboard: renderBlackboard" in js and 'data-view="blackboard"' in (
        _ROOT / "src" / "agent_mailbox" / "workbench_assets" / "index.html"
    ).read_text(encoding="utf-8")


def test_discovery_states_its_own_boundary():
    """第 10a 条：发现结果必须**写明边界** ✓（"未发现的不会自动出现" + 指路手工登记 ✓）。

    为什么用测试钉住：这句文案是"用户自主权"的**可见载体** ✓ —— 删掉它，用户就只知道
    "扫描没发现 = 不支持我" ✗（老板原话的痛点 ✓）。
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "未发现的不会自动出现" in js, "边界必须写明 ✗"
    assert "手工登记" in js, "必须指路手动添加（自救路径 ✓）"
    assert "入口需真实存在且可执行" in js, "必须写清手动登记的前置条件（否则用户会白填 ✗）"


def test_member_rows_expose_mailbox_key_status():
    """缺口 2 回归 ✓：成员行必须**显式标出钥匙状态** ✗ 并能一键签发 ✓。

    （用户"加了人却收不到信"的真因 ✓：`add_project_member` 只建成员关系 ✓ **不签钥匙** ✗
      而界面里当时既没有状态也没有按钮 ⇒ 只能靠我用脚本签 ✓ 现在闭环了 ✓）
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "attachKeyStatus" in js, "缺钥匙状态渲染 ✓"
    assert "未签发钥匙" in js, "必须**显式**标出未签发 ✗ 不许让用户猜 ✓"
    assert "api.createMailSession" in js, "必须能一键签发（人侧点击 = 显式确认 ✓）"
    assert "api.mailSessions" in js, "必须读现有钥匙状态 ✓"
    assert "token 绝不显示" in js, "签发后只显示到期时间 ✓ 绝不显示 token ✓"
    assert "有效至" in js, "要显示钥匙到期时间（实测返回里有 expires_at ✓ 没有路径字段 ✗）"


def test_key_page_shows_per_project_keys():
    """钥匙管理页 ✓（老板判据：界面上的功能都要真能用 ✗）—— 回答"谁在哪个项目、持哪把钥匙、何时到期" ✓。"""
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "function renderKeys(" in js, "缺钥匙页渲染 ✓"
    assert "api.mailSessions" in js and "api.revokeMailSession" in js, "必须能看 + 能撤销 ✓"
    assert "未签发" in js, "缺钥匙的行必须**显式**标出 ✗"
    assert "token 绝不显示" in js, "签发后绝不显示 token ✓"


def test_sidebar_cannot_overlap_when_window_is_short():
    """侧栏矮窗回归 ✓（老板实测：窗口缩小 ⇒ 底部「管理」叠到中间菜单上 ✗）。

    真机量过 ✓（Playwright · 900×400 · 真实工作台外壳）：
      · 修前 ⇒ `.sidebar-bottom` 与**全部 7 个中段菜单项**各叠 ~38px ✗（中段被 flex 挤瘪 ✓）
      · 修后 ⇒ `overlapCount = 0` ✓ 底部块排在中段之后（588..971 ✓）· 侧栏 `overflow-y:auto` 可滚 ✓
    这里锁住**结构性前提** ✓（CSS 一旦回退成 flex 列 / 加回 margin-top:auto 就红灯 ✓）。
    """
    css = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.css").read_text(
        encoding="utf-8"
    )
    sidebar = css[css.index(".sidebar {") : css.index(".sidebar {") + 400]
    assert "overflow-y: auto" in sidebar, "侧栏必须自身可滚 ✓（否则矮窗下内容被裁 ✗）"
    assert "display: flex" not in sidebar, (
        "侧栏不得用 flex 列 ✗（会把中段挤瘪、顺序错乱 ⇒ 底部压上来 ✗）"
    )
    bottom = css[css.index(".sidebar-bottom {") :].split("}")[0]
    assert "margin-top: auto" not in bottom, (
        "底部块不得再用 margin-top:auto ✗（矮窗下会与中段重叠 ✗）"
    )


def test_about_shows_build_provenance():
    """AM-06 回归 ✓：关于页必须显示**构建溯源** ✗（revision / 资源哈希 / home / schema ✓）。

    Codex 实测：安装包里的 `workbench.js` 与源码 SHA256 **不同** ✗ ⇒ 修复到不了用户 ✓
    ⇒ 产品必须能自证"你打开的到底是哪份代码" ✓ 否则每次"改了没变化"都无从判断 ✓。
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "function buildInfoSection(" in js, "关于页必须有构建信息块 ✓"
    for field in ("源码 revision", "数据目录 home", "库结构 schema"):
        assert field in js, f"缺字段：{field} ✓"
    py = (_ROOT / "src" / "agent_mailbox" / "workbench.py").read_text(encoding="utf-8")
    assert "def build_info(" in py and '"build": build_info(store)' in py, (
        "bootstrap 必须提供 build ✓"
    )
    assert "workbench.js" in py and "sha256" in py, "必须对界面资源取哈希 ✓（与安装包逐字节可比 ✓）"


def test_runtime_mode_is_visible():
    """Gate 2 (spec section 3): the UI must say whether you run the artifact or the working tree.

    Why it matters: tonight the live workbench silently ran this repo checkout,
    so "fixes never showed up" was unknowable. A visible mode makes it self-evident.
    """
    py = (_ROOT / "src" / "agent_mailbox" / "workbench.py").read_text(encoding="utf-8")
    assert 'info["mode"]' in py, "后端必须给出运行形态 ✓"
    assert '"packaged"' in py and '"source"' in py, "必须区分成品与工作树 ✓"
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "运行形态" in js, "关于页必须显示运行形态 ✓"
    assert "工作树" in js, "跑工作树时必须**响亮**写出来 ✗"


def test_narrow_window_never_hides_the_message_body():
    """AM-09 regression: a narrow window must never hide the reading pane (Codex).

    Bug evidence: at 900px the message body measured 0 by 0 with no reply action;
    at 1200px the same letter was readable and replyable.
    Targeted assertion (no brace walking): the reading pane rule must never be
    `display: none`, and the narrow media query must stack it instead.
    """
    import re

    css = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.css").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"\.mailbox-reading\s*\{[^}]*display:\s*none", css), (
        "阅读区绝不许被隐藏 ✗（AM-09）"
    )
    narrow = re.search(r"@media \(max-width: 1100px\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert narrow, "必须存在 1100px 的窄窗规则 ✓"
    assert "grid-template-columns: minmax(0, 1fr)" in narrow.group(1), "窄窗应改为竖排 ✓"


def test_employee_search_input_survives():
    """AM-10 regression: the employee search box must stay a sibling of the icon.

    My AM-08 paren repair once wrote `icon("search", search)`, which swallows the
    input as an unused argument: the toolbar rendered zero search inputs.
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert 'icon("search", search)' not in js, "搜索框不得被当成 icon 的参数 ✗（AM-10）"
    assert 'icon("search"), search' in js, "搜索框必须是兄弟节点 ✓"


def test_mode_is_not_decided_by_build_info_presence(tmp_path):
    """AM-06 (Codex repro): build-info.json must NOT decide the runtime shape.

    Codex reproduced: running from an isolated source directory reported `packaged`
    merely because build-info.json existed. Hash equality proves content equality only,
    and a plist may launch anything -- so the shape comes from the loaded code path,
    and must be `unknown` when that is not conclusive.
    """
    import sys as _sys

    from agent_mailbox.workbench import build_info
    from agent_mailbox.workbench_store import WorkbenchStore

    # **不许依赖本机已打过包** ✗（Codex：干净检出里该测试红 ✓）
    # ⇒ 只断言"仓库运行 ⇒ source" ✓ 并由**源码层面**证明判据不再看 build-info.json ✓
    info = build_info(WorkbenchStore(tmp_path / "home"))
    assert info["mode"] == "source", (
        f"仓库里运行必须是 source ✓（旧判据会误报 packaged ✗，实测 {info['mode']}）"
    )
    runtime = info["runtime"]
    assert runtime["code_path"].endswith("src/agent_mailbox/workbench.py"), runtime
    assert runtime["executable"] == _sys.executable
    assert isinstance(runtime["argv"], list)
    source = (_ROOT / "src" / "agent_mailbox" / "workbench.py").read_text(encoding="utf-8")
    assert 'info["mode"] = "unknown"' in source, "证据不足时必须能给出「未确认」✗"
    # 判据只能来自**实际加载路径** ✓ 绝不再看 `build-info.json` 是否存在 ✗（Codex 复现的假阳性 ✓）
    mode_block = source[
        source.index('info["runtime"] = {') : source.index('info["mode"] = "unknown"')
    ]
    assert "build-info" not in mode_block, "形态判定不得引用 build-info.json ✗"
    assert "code_path" in mode_block, "形态判定必须基于实际加载的代码路径 ✓"


def test_employee_filters_are_not_nested_inside_the_search_label():
    """AM-11 (Codex): filters and the count must NOT live inside the 360px search label.

    Consequences of nesting them: the toolbar squeezes even at 1200px, and every filter
    button inherits the label's text as its accessible name ("13 个入口"), so the four
    filters become indistinguishable in the accessibility tree.
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    line = next(l for l in js.split("\n") if "employee-directory-toolbar" in l)
    label = line[line.index('el("label"') : line.index("filters,")]
    assert "filters" not in label.split("),")[0], "筛选组不得在 label 内 ✗（AM-11）"
    assert 'employee-search" }, icon("search"), search),' in line, "label 只应包输入框 ✓"
    assert "employee-result-count" in line, "计数应有自己的 class 以便单独排版 ✓"
    css = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.css").read_text(
        encoding="utf-8"
    )
    assert ".employee-result-count { white-space: nowrap; }" in css, "计数不得逐字换行 ✗"


def test_overview_uses_its_own_executing_list():
    """AM-12: the overview must use its own executing list (queued is not 正在做).

    This test also guards the dangling-reference bug: using `executing.length` while
    the declaration is missing would raise ReferenceError in the browser.
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    decl = [l for l in js.split("\n") if "const executing = tasks.filter" in l]
    assert decl, "必须声明 executing ✓（否则页面 ReferenceError ✗）"
    assert '"queued"' not in decl[0], "执行中不得含排队 ✗（AM-12）"
    assert '["starting", "running"]' in decl[0], "执行中 = starting/running ✓"
    assert 'statCard(t("正在做", "In progress"), executing.length' in js, "概览卡片必须用它 ✓"
    # **Codex 19:20**：卡片改了、**下方同名列表**却还用 running ⇒ 一处 0 一处 1 自相矛盾 ✗
    assert 'taskSection(t("正在做", "In progress"), executing' in js, "列表必须与卡片同源 ✓"
    assert 'taskSection(t("正在做", "In progress"), running' not in js, "列表不得再用 running ✗"


def test_desktop_marker_is_injected_only_by_the_native_shell():
    """AM-14 (Codex): the desktop marker comes from the WKWebView shell, never from mode.

    A plain browser can also reach the packaged app's service, so `mode=packaged` cannot
    distinguish App from Web. The native shell injects the marker at document start, and
    popover support is feature-detected inside the real WKWebView.
    """
    py = (_ROOT / "src" / "agent_mailbox" / "workbench_desktop.py").read_text(encoding="utf-8")
    assert "DESKTOP_MARKER_SCRIPT" in py, "外壳必须注入桌面标记 ✓"
    assert 'dataset.shell = "desktop"' in py, "标记必须是 dataset.shell ✓"
    assert "WKUserScript" in py and "addUserScript_" in py, "必须用 WKUserScript 注入 ✓"
    assert "togglePopover" in py, "Popover 能力必须**实探测** ✗（不能凭系统版本假定 ✓）"
    # **按代码判，不按注释判** ✗（我上一版把 `mode=packaged` 写进注释 ⇒ 断言自毁 ✓ 今天第二次同类 ✗）
    code = "\n".join(l for l in py.split("\n") if not l.lstrip().startswith("#"))
    assert "mode=packaged" not in code, "代码不得用 mode 判断桌面/浏览器 ✗（Codex）"
    marker = code[code.index("DESKTOP_MARKER_SCRIPT") : code.index("def install_desktop_marker")]
    assert "mode" not in marker, "标记脚本不得引用 mode ✗"


def test_project_switcher_popover_is_app_only():
    """AM-14 (Codex): the floating project switcher is App-only and Web stays untouched.

    Requirements checked here (source-level): gate on the shell marker (never on mode),
    prefer the Popover API with a detached-container fallback, close on outside/Esc/select,
    return focus to the trigger, reposition on resize/scroll, and leave the browser path
    (the inline details switch) exactly as it was.
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert 'dataset.shell === "desktop"' in js, "必须以**外壳标记**门控 ✓（不是 mode ✗）"
    assert 'dataset.popoverApi === "yes"' in js, "Popover 能力用外壳探测结果 ✓"
    assert 'setAttribute("popover", "auto")' in js, (
        "优先 Popover API ✓（top layer ⇒ 不受祖先裁切 ✓）"
    )
    assert "document.body.append(host)" in js, "退化路径须放**独立容器** ✓（仅抬 z-index 不可靠 ✗）"
    css = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.css").read_text(
        encoding="utf-8"
    )
    assert ".project-popover" in css and "overflow-y: auto" in css, "浮层自身可滚 ✓"
    assert "white-space: normal" in css, "长名允许换行 ✓"
    # 浏览器路径不动 ✓：内联 details 仍然存在 ✓ 且桌面判定只在浮层相关处使用 ✓
    assert '<details class="project-switcher"' in (
        _ROOT / "src" / "agent_mailbox" / "workbench_assets" / "index.html"
    ).read_text(encoding="utf-8")


def test_popover_capture_listener_lets_the_plus_button_through():
    """AM-14 regression (Codex): the capture listener must NOT swallow the + button.

    The new-project (＋) control lives *inside* `.project-switcher > summary`. If the
    capture listener intercepted every click on the summary, ＋ would merely toggle the
    popover and creating a project in the App would break. Interactive controls keep
    their own path.
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    guard = '[data-action="new-project"], button, a, input, [role="button"]'
    assert guard in js, "必须显式放行 summary 内的交互控件 ✓（否则 ＋ 被拦 ✗）"
    idx = js.index(guard)
    assert "return;" in js[idx : idx + 400], "放行必须是提前 return ✓"
    assert "projectPlus" in js, "＋ 的原有接线必须保留 ✓"


def test_new_project_button_reaches_the_main_dispatcher():
    """HS/Codex: clicking ＋ must reach the delegated dispatcher (bubble phase).

    The ＋ control had its own listener calling `event.stopPropagation()`, which cut the click
    before the document-level dispatcher could run `actions["new-project"] = openProjectForm`.
    The popover part was fine; the actual create-project flow never fired. `preventDefault`
    must stay (otherwise ＋ also opens the project dropdown).
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    plus = next(l for l in js.split("\n") if "projectPlus.addEventListener" in l)
    assert "stopPropagation" not in plus, "＋ 监听不得截断事件 ✗（HS：主委托收不到 ⇒ 点＋没反应 ✓）"
    assert "preventDefault" in plus, "必须保留 preventDefault ✗（否则点＋会展开项目下拉 ✓）"
    assert '"new-project": openProjectForm' in js, "主委托里必须有 new-project 的派发点 ✓"


def test_sidebar_is_two_section_and_app_gated():
    """AM-17：CSS 注释必须配对（残留文本会吃掉 `.sidebar` 规则）＋ 两段式必须真的写下。

    我删 sticky 规则时误删注释开头 `/*` ⇒ 残留中文被并进选择器 ⇒ `.sidebar{display:flex…}`
    整条被解析器丢弃。判据只做**确定可靠**的检查（不做全量选择器字符集校验 —— 那会误伤
    @media/@supports 等合法写法），并把"渲染是否生效"留给新包实测。
    """
    css = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.css").read_text(
        encoding="utf-8"
    )
    assert css.count("/*") == css.count("*/"), (
        f"CSS 注释必须配对 ✗（/* {css.count('/*')} vs */ {css.count('*/')}）—— AM-17 的直接判据"
    )
    assert ".sidebar { display: flex; flex-direction: column; overflow: hidden; }" in css, (
        "父侧栏必须是 flex 列 ✓"
    )
    assert ".sidebar-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto;" in css, (
        "中段是唯一滚动区 ✓"
    )
    bottoms = [chunk for chunk in css.split("}") if chunk.lstrip().startswith(".sidebar-bottom {")]
    assert bottoms, "必须有 .sidebar-bottom 规则 ✓"
    assert any("flex: none" in b for b in bottoms), "底部留在布局里 ✓"
    assert not any("position: sticky" in b for b in bottoms), "底部不得 sticky 盖住菜单 ✗"
    html = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "index.html").read_text(
        encoding="utf-8"
    )
    o = html.index('<div class="sidebar-scroll">')
    c2 = html.index("</div>", o)
    b2 = html.index('class="sidebar-bottom"')
    assert o < c2 < b2, "底部区必须在滚动区之外 ✓"
    assert 'html[data-shell="desktop"] .sidebar .nav-item { min-height: 32px;' in css, (
        "压缩须门控桌面 ✓"
    )
    assert 'html[data-shell="desktop"] .sidebar-scroll::-webkit-scrollbar' in css, (
        "隐藏滚动条须门控桌面 ✓"
    )


def test_arg_taking_actions_are_wrapped_so_the_clicked_button_is_not_the_mode():
    """AM-15 (Codex/HS): the delegate calls `actions[name](target)`.

    A bare reference therefore passes the clicked button as the first argument. For
    `openTaskForm(mode = "mailbox")` that silently switched the form into the managed-execution
    branch (real App: clicking 新任务 jumped to 项目成员). Any action whose function declares a
    parameter must be wrapped, so button identity can never become a mode.
    """
    js = (_ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js").read_text(
        encoding="utf-8"
    )
    assert "actions[target.dataset.action](target)" in js, "委托确实会把 target 作为第一参数 ✓"
    assert '"new-task": () => openTaskForm("mailbox")' in js, "new-task 必须显式包参 ✗（AM-15）"
    # 动作表里不得再出现"裸引用"带参函数 ✗
    table = js[js.index("const actions = {") : js.index("};", js.index("const actions = {"))]
    body = js
    for name in re.findall(r"^function (\w+)\(([^)]*)\)", body, re.MULTILINE):
        fn, params = name
        if not params.strip():
            continue
        assert f"{fn}," not in table and f"{fn}\n" not in table, f"{fn} 带参 ⇒ 动作表里必须包一层 ✗"


def test_overview_cards_share_the_drilldown_predicate():
    js = _js()
    assert 'awaiting.length, "awaiting"' in js
    assert 'executing.length, "executing"' in js
    assert 'filter === "awaiting"' in js
    assert 'filter === "executing"' in js
    assert "liveStatuses.has(task.status)" in js
