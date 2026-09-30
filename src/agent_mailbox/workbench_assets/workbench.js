import { api, ApiError } from "./api.js";

const english = !navigator.language.toLowerCase().startsWith("zh");
const t = (zh, en) => english ? en : zh;
document.documentElement.lang = english ? "en" : "zh-CN";
document.title = t("工作台 · agent-mailbox", "Workbench · agent-mailbox");

const root = document.getElementById("view-root");
const notice = document.getElementById("global-notice");
const formDialog = document.getElementById("form-dialog");
const detailDialog = document.getElementById("detail-dialog");
const formContent = document.getElementById("form-dialog-content");
const detailContent = document.getElementById("detail-dialog-content");
const state = {
  data: null, view: "employees", projectId: "", refreshing: false,
  taskFilter: "all", search: "", discovered: null,
  detailTaskId: null, detailRequest: 0, detailFingerprint: "", detailError: "",
  runtimeInstalling: false, error: null, toastTimer: null,
  changesPending: false, changeTimer: null, streamError: false,
  governance: null, governanceError: null, governanceLoading: false, governanceQueued: false,
  applicationStopping: false, applicationStopped: false,
  taskFormUpdate: null,
  messageDrafts: {}, messages: {}, messagesLoading: false, messagesError: null, dragEmployeeId: null,
};
try { state.projectId = sessionStorage.getItem("agent-mailbox.workbench.project") || ""; } catch { /* optional */ }

const navLabels = {
  overview: t("项目概览", "Overview"), tasks: t("任务", "Tasks"),
  employees: t("所有员工", "All employees"), members: t("项目成员", "Project members"), messages: t("项目消息", "Messages"), resources: t("资料与记忆", "Resources & memory"),
  devices: t("设备", "Devices"),
};
const statusLabels = {
  queued: t("排队中", "Queued"), starting: t("正在启动", "Starting"),
  running: t("正在执行", "Running"), waiting_approval: t("等待授权", "Needs permission"),
  review: t("待验收", "For review"), done: t("已验收", "Accepted"),
  failed: t("执行失败", "Failed"), cancelled: t("已取消", "Cancelled"),
  interrupted: t("执行中断", "Interrupted"), ready: t("可接任务", "Ready"),
  available: t("已发现", "Detected"), installed: t("已发现", "Detected"),
  configured: t("已接入", "Connected"), idle: t("空闲", "Idle"),
  busy: t("工作中", "Working"), online: t("在线", "Online"),
  offline: t("离线", "Offline"), unknown: t("状态待确认", "Unconfirmed"),
  revoked: t("已撤销", "Revoked"), paired: t("已配对", "Paired"),
  error: t("需要处理", "Needs attention"), needs_auth: t("需要登录", "Sign-in needed"), auth_required: t("需要登录", "Sign-in needed"),
  not_installed: t("未安装", "Not installed"), missing: t("未安装", "Not installed"),
  unavailable: t("未就绪", "Unavailable"), unsupported: t("暂不支持", "Unsupported"),
  pending: t("等待确认", "Pending"), allowed: t("已允许", "Allowed"), denied: t("已拒绝", "Denied"),
  authenticated: t("已登录", "Signed in"), signed_in: t("已登录", "Signed in"), not_required: t("无需登录", "Sign-in not required"),
  active: t("在岗", "Active"), paused: t("已暂停", "Paused"), retired: t("已退役", "Retired"), expired: t("已过期", "Expired"),
};
const liveStatuses = new Set(["queued", "starting", "running", "waiting_approval"]);
const attentionStatuses = new Set(["waiting_approval", "failed", "interrupted"]);

// All server-controlled values go through text nodes or DOM properties.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "value") node.value = value;
    else if (key === "disabled" || key === "hidden" || key === "checked") node[key] = Boolean(value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  function append(child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(append); return; }
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  children.forEach(append);
  return node;
}

const iconPaths = {
  plus: ["M12 5v14M5 12h14"], close: ["m6 6 12 12M18 6 6 18"],
  arrow: ["m9 5 7 7-7 7"], check: ["m5 12 4 4L19 6"],
  folder: ["M3 7h7l2 2h9v11H3zM3 7V4h7l2 3"],
  users: ["M15 21v-2a6 6 0 0 0-12 0v2m13-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5", { circle: { cx: 9, cy: 8, r: 3 } }],
  task: ["m4 7 2 2 3-4m-5 12 2 2 3-4M12 7h8M12 17h8"],
  play: ["m9 5 10 7-10 7z"], clock: [{ circle: { cx: 12, cy: 12, r: 8 } }, "M12 7v5l3 2"],
  review: ["M7 3h10v3H7zM7 5H4v16h16V5h-3m-9 8 3 3 5-6"],
  warning: ["m12 3 10 18H2zM12 9v4M12 17h.01"],
  pause: ["M8 5v14M16 5v14"], stop: ["M6 6h12v12H6z"],
  document: ["M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"],
  memory: ["M12 3v18m0-17C9 2 5 4 5 7c-3 1-3 6 0 7-2 3 1 6 4 6l3-2m0-14c3-2 7 0 7 3 3 1 3 6 0 7 2 3-1 6-4 6l-3-2"],
  monitor: ["M3 4h18v12H3zM8 21h8m-4-5v5"],
  shield: ["m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM9 12l2 2 4-5"],
  link: ["m10 14 4-4m-6 5-2 2a4 4 0 0 1-5-5l4-4a4 4 0 0 1 5 0m4 1 2-2a4 4 0 0 1 5 5l-4 4a4 4 0 0 1-5 0"],
  moon: ["M20 13a8 8 0 0 1-9-9 8 8 0 1 0 9 9Z"],
  sun: [{ circle: { cx: 12, cy: 12, r: 4 } }, "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"],
  download: ["M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"],
  search: [{ circle: { cx: 10, cy: 10, r: 6 } }, "m15 15 6 6"],
  trash: ["M3 6h18M8 6V3h8v3M5 6l1 15h12l1-15M10 10v7M14 10v7"],
  refresh: ["M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 2M4 16l2 2a7 7 0 0 0 12-1"],
};
function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const item of iconPaths[name] || iconPaths.document) {
    const shape = document.createElementNS(ns, typeof item === "string" ? "path" : "circle");
    if (typeof item === "string") shape.setAttribute("d", item);
    else for (const [key, value] of Object.entries(item.circle)) shape.setAttribute(key, value);
    svg.append(shape);
  }
  return svg;
}
function button(label, action, options = {}) {
  return el("button", { type: "button", class: `button ${options.class || ""}`, "data-action": action, disabled: options.disabled, ...options.attrs }, options.icon ? icon(options.icon) : null, label);
}
function empty(title, description, action, label, symbol = "folder") {
  return el("div", { class: "large-empty" }, icon(symbol), el("h2", {}, title), el("p", {}, description), action ? button(label, action, { class: "primary", icon: "plus" }) : null);
}
function tag(status) {
  return el("span", { class: `status-tag ${Object.hasOwn(statusLabels, status) ? status : "unknown"}` }, statusLabels[status] || t("状态待确认", "Unconfirmed"));
}
function avatar(name) { return el("span", { class: "avatar", "aria-hidden": "true" }, (name || "?").slice(0, 2)); }
function project() { return state.data?.projects.find((item) => item.id === state.projectId) || null; }
function projectEmployees() {
  return (state.data?.employees || []).filter((employee) => employee.project_id === state.projectId || employee.project_ids?.includes(state.projectId));
}
function assignableEmployees() { return projectEmployees().filter((employee) => employee.execution_supported === true && (!employee.lifecycle || employee.lifecycle === "active")); }
function projectTasks() { return (state.data?.tasks || []).filter((task) => task.project_id === state.projectId); }
function employeeName(id) { return state.data?.employees.find((employee) => employee.id === id)?.name || t("未分配员工", "Unassigned"); }
function deviceName(id) { return state.data?.devices.find((device) => device.id === id)?.name || (state.data?.node?.id === id ? state.data.node.name : t("设备未确认", "Device unconfirmed")); }
function formatDate(value, detailed = false) {
  if (!value) return t("时间未记录", "Time not recorded");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("时间未记录", "Time not recorded");
  return new Intl.DateTimeFormat(english ? "en" : "zh-CN", { month: "short", day: "numeric", hour: detailed ? "2-digit" : undefined, minute: detailed ? "2-digit" : undefined }).format(date);
}
function errorText(error) {
  if (error.status === 401 || (error.status === 403 && ["UNAUTHORIZED", "authorization_required"].includes(error.code))) return t("此页面没有访问授权。请从本机工作台入口重新打开。", "This page is not authorized. Reopen it from the workbench launcher on this computer.");
  const messages = {
    authorization_required: t("请从本机工作台入口重新打开页面以连接服务。", "Reopen this page from the workbench launcher to connect."),
    request_timeout: t("服务响应超时，请稍后重试。", "The service took too long to respond. Try again."),
    connection_failed: t("无法连接工作台服务，请确认它正在运行后重试。", "Could not connect to the workbench. Check that it is running and try again."),
    invalid_response: t("服务返回了无法读取的数据，请刷新后重试。", "The service returned an unreadable response. Refresh and try again."),
    request_failed: t("操作没有完成，请重试。", "The operation did not complete. Try again."),
  };
  return messages[error.message] || error.message || t("操作没有完成，请重试。", "The operation did not complete. Try again.");
}
function humanDetail(value) {
  const messages = {
    "Execution runtime is ready.": t("执行环境已准备好。", "Execution runtime is ready."),
    "Install the execution runtime before starting an employee.": t("完成一次准备后，员工就可以在项目里执行任务。", "Set up execution once before starting employees."),
    "Install this supported agent to use it here.": t("需要先安装这个 AI 工具。", "Install this AI tool first."),
    "Installed; execution has not been verified yet.": t("已安装，首次任务会验证能否执行。", "Installed; the first task will verify execution."),
    "Signed in; the first task will verify execution.": t("已登录，首次任务会验证能否执行。", "Signed in; the first task will verify execution."),
    "Installed; sign-in status could not be confirmed.": t("已安装，登录状态尚未确认。", "Installed; sign-in status is not confirmed."),
    "Installed; sign-in check timed out or was unavailable.": t("已安装，本次登录检查未完成，请重新检查。", "Installed; sign-in check did not complete. Check again."),
    "Sign in with Codex, then discover again.": t("先登录 Codex，再重新检查。", "Sign in to Codex, then check again."),
    "Sign in with Claude Code, then discover again.": t("先登录 Claude Code，再重新检查。", "Sign in to Claude Code, then check again."),
  };
  return messages[value] || value;
}
function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  const region = document.getElementById("toast-region");
  const close = el("button", { type: "button", class: "icon-button", "aria-label": t("关闭提示", "Dismiss notification") }, icon("close"));
  close.addEventListener("click", () => region.replaceChildren());
  region.replaceChildren(el("div", { class: `toast${isError ? " error" : ""}` }, el("span", {}, message), close));
  state.toastTimer = window.setTimeout(() => region.replaceChildren(), isError ? 10000 : 5000);
}
function setNotice(message, isError = false) {
  notice.replaceChildren();
  notice.hidden = !message;
  if (!message) return;
  notice.className = `notice${isError ? " error" : ""}`;
  notice.append(el("span", {}, message), button(t("重试", "Retry"), "refresh", { class: "small", icon: "refresh" }));
}
function updateConnection(connected) {
  document.getElementById("service-dot").className = `status-dot ${connected ? "connected" : "error"}`;
  document.getElementById("service-label").textContent = connected ? t("工作台已连接", "Workbench connected") : t("服务未连接", "Disconnected");
  const connection = document.getElementById("connection-label");
  connection.replaceChildren(el("span", { class: `status-dot ${connected ? "connected" : "error"}` }), el("span", {}, connected ? t("已连接", "Connected") : t("未连接", "Disconnected")));
}
function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const control = document.getElementById("theme-button");
  control.replaceChildren(icon(dark ? "sun" : "moon"));
  control.setAttribute("aria-label", dark ? t("切换为浅色外观", "Switch to light appearance") : t("切换为深色外观", "Switch to dark appearance"));
}
function setSidebar(open) {
  const sidebar = document.getElementById("sidebar");
  const returnFocus = !open && sidebar.contains(document.activeElement) && matchMedia("(max-width: 760px)").matches;
  sidebar.classList.toggle("open", open);
  document.getElementById("sidebar-backdrop").hidden = !open;
  document.getElementById("sidebar-toggle").setAttribute("aria-expanded", String(open));
  if (open) sidebar.querySelector(".brand").focus();
  else if (returnFocus) document.getElementById("sidebar-toggle").focus();
}
function selectProject(id) {
  if (!state.data?.projects.some((item) => item.id === id)) return;
  state.projectId = id;
  state.view = "overview";
  state.search = "";
  state.taskFilter = "all";
  state.discovered = null;
  state.governance = null;
  state.governanceError = null;
  try { sessionStorage.setItem("agent-mailbox.workbench.project", id); } catch { /* optional */ }
  render();
  setSidebar(false);
  if (state.view === "members") loadGovernance();
}
function selectView(view) {
  if (!Object.hasOwn(navLabels, view)) return;
  state.view = view;
  state.search = "";
  render();
  setSidebar(false);
  document.getElementById("main").focus({ preventScroll: true });
  if (view === "members") loadGovernance();
  if (view === "messages") loadMessages();
}

function renderSidebar() {
  const list = document.getElementById("project-list");
  if (state.data?.projects.length) {
    list.replaceChildren(...state.data.projects.map((item) => el("button", {
      type: "button", class: `project-item${item.id === state.projectId ? " selected" : ""}`,
      "data-project": item.id, "aria-pressed": String(item.id === state.projectId), title: item.name,
    }, el("span", { class: "project-symbol", "aria-hidden": "true" }, (item.name || "P").slice(0, 1)), el("span", { class: "project-name" }, item.name))));
  } else list.replaceChildren(el("p", { class: "sidebar-empty" }, state.data ? t("还没有项目，点击 + 开始。", "No projects yet. Select + to begin.") : t("连接后显示你的项目。", "Your projects appear when connected.")));
  for (const control of document.querySelectorAll("[data-view]")) {
    const active = control.dataset.view === state.view;
    control.classList.toggle("selected", active);
    if (active) control.setAttribute("aria-current", "page");
    else control.removeAttribute("aria-current");
  }
  for (const control of list.querySelectorAll("[data-project]")) {
    const eligible = () => state.data?.employees.find((employee) => employee.id === state.dragEmployeeId && employee.lifecycle !== "retired");
    control.addEventListener("dragover", (event) => {
      if (!eligible()) return;
      event.preventDefault(); event.dataTransfer.dropEffect = "copy"; control.classList.add("drag-target");
    });
    control.addEventListener("dragleave", () => control.classList.remove("drag-target"));
    control.addEventListener("drop", async (event) => {
      event.preventDefault(); control.classList.remove("drag-target");
      const employeeId = event.dataTransfer?.getData("text/plain");
      const employee = eligible();
      if (!employee || employee.id !== employeeId || !state.data.projects.some((item) => item.id === control.dataset.project)) {
        showToast(t("无法加入：请从在册员工拖动，退役身份不能再次加入。", "Unable to add: drag a registered employee. Retired identities cannot rejoin."), true); return;
      }
      const projectId = control.dataset.project;
      if (employee.project_id === projectId || employee.project_ids?.includes(projectId)) { showToast(t("这名员工已在项目中。", "This employee is already a project member.")); return; }
      control.disabled = true;
      try { await api.addProjectMember(projectId, employeeId); await refresh(); showToast(t(`${employee.name} 已加入项目。`, `${employee.name} joined the project.`)); }
      catch (error) { showToast(errorText(error), true); control.disabled = false; }
      finally { state.dragEmployeeId = null; }
    });
  }
  const current = project();
  document.getElementById("breadcrumb-project").textContent = state.view === "employees" ? t("工作台", "Workbench") : current?.name || t("工作台", "Workbench");
  document.getElementById("breadcrumb-page").textContent = navLabels[state.view];
  document.getElementById("task-nav-count").textContent = current ? String(projectTasks().filter((task) => task.status !== "done" && task.status !== "cancelled").length) : "";
}
function heading(title, description, actions = []) {
  return el("div", { class: "page-heading" }, el("div", {}, el("h1", {}, title), description ? el("p", { class: "subheading" }, description) : null), actions.length ? el("div", { class: "page-heading-actions" }, actions) : null);
}
function runtimeNotice({ force = false } = {}) {
  const runtime = state.data?.runtime;
  const people = assignableEmployees();
  if (!force && people.length && people.every((employee) => employee.node_id && employee.node_id !== state.data.node?.id)) return null;
  if (!runtime || (runtime.installed && runtime.node_available)) return null;
  const installation = runtime.install;
  const installing = state.runtimeInstalling || installation?.status === "installing";
  const failed = installation?.status === "failed";
  const title = installing ? t("正在准备执行环境", "Preparing the execution environment") : failed ? t("执行环境准备失败", "Execution setup failed") : runtime.installed ? t("执行服务需要处理", "Execution service needs attention") : t("让员工开始工作，还需要准备执行环境", "Prepare the environment to let employees work");
  return el("div", { class: "runtime-notice" }, icon("download"), el("div", {}, el("strong", {}, title), el("p", {}, installing ? t("完成后这里会自动更新，你可以先整理项目和资料。", "This will update automatically. You can organize your project and resources meanwhile.") : installation?.error?.message || humanDetail(runtime.detail) || t("一次准备，后续任务会使用同一套环境。", "Set it up once for future tasks."))), button(installing ? t("准备中…", "Preparing…") : failed ? t("重试准备", "Retry setup") : t("准备执行环境", "Set up execution"), "install-runtime", { class: "small", disabled: installing }));
}
function taskIcon(status) {
  const name = status === "done" ? "check" : status === "review" ? "review" : status === "waiting_approval" ? "shield" : status === "running" ? "play" : attentionStatuses.has(status) ? "warning" : "clock";
  return el("span", { class: `task-status-icon ${statusLabels[status] ? status : "unknown"}`, "aria-hidden": "true" }, icon(name));
}
function taskRow(task) {
  return el("button", { type: "button", class: "task-row", "data-task": task.id, "aria-label": `${task.title} · ${statusLabels[task.status] || statusLabels.unknown}` },
    taskIcon(task.status), el("div", {}, el("div", { class: "task-title" }, task.title), el("div", { class: "task-meta" }, el("span", {}, employeeName(task.assignee_id)), el("span", {}, formatDate(task.updated_at || task.created_at)))),
    el("div", { class: "task-row-end" }, tag(task.status), icon("arrow")));
}
function taskSection(title, tasks, explanation, symbol) {
  return el("section", { class: "work-section" }, el("div", { class: "section-heading" }, el("h2", { class: "section-title" }, icon(symbol), title, el("span", { class: "section-count" }, tasks.length))),
    tasks.length ? el("div", { class: "task-list" }, tasks.slice(0, 6).map(taskRow)) : el("div", { class: "section-empty" }, icon("check"), explanation));
}
function renderWelcome() {
  const step = (number, title, description, action, label, active) => el("div", { class: "onboarding-step" }, el("span", { class: `step-number${active ? " active" : ""}`, "aria-hidden": "true" }, number), el("div", { class: "step-body" }, el("h2", {}, title), el("p", {}, description), button(label, action, { class: active ? "primary" : "", disabled: !state.data || !active, icon: active ? "folder" : undefined })));
  return [heading(t("把工作交给你的 AI 团队", "Put your AI team to work"), t("从一个项目开始，员工、任务和工作记录都留在这里。", "Start with one project. Keep employees, tasks, and work records together.")),
    el("div", { class: "welcome-layout" }, el("section", { class: "onboarding-panel", "aria-label": t("首次使用", "Getting started") },
      step(1, t("选择项目", "Choose a project"), t("选择这次工作的文件夹，给项目起个名字。", "Choose the folder for this work and give the project a name."), "new-project", t("选择项目文件夹", "Choose project folder"), true),
      step(2, t("加入已有员工", "Connect your existing employees"), t("发现这台设备上已有的 AI 工具，把需要的员工加入项目。", "Discover AI tools on this computer and add employees to your project."), "discover", t("发现员工", "Discover employees"), false),
      step(3, t("交代第一件事", "Assign the first task"), t("写清要做什么、做到什么程度，再把任务交给员工。", "Describe the work and what a good result looks like, then assign it."), "new-task", t("创建任务", "Create task"), false)),
    el("aside", { class: "welcome-note" }, icon("users"), el("h2", {}, t("你负责目标，工作记录在这里", "You set the goal. The work stays here.")), el("p", {}, t("员工可以交接工作、共享资料。你只需要知道正在做什么、哪里需要你、交付了什么。", "Employees can hand off work and share context. See what is running, what needs you, and what was delivered.")),
      el("ul", {}, el("li", {}, icon("folder"), t("围绕项目组织任务与资料", "Organize tasks and context by project")), el("li", {}, icon("review"), t("交付后由你验收", "You review completed work")), el("li", {}, icon("shield"), t("任务默认只读，授权由你决定", "Read-only by default. You control permissions"))),
      el("p", { class: "welcome-footnote" }, t("当前显示真实服务状态，没有预置示例员工或任务。", "This page shows real service state, with no sample employees or tasks."))))];
}
function projectContext() {
  const current = project();
  const people = projectEmployees();
  const resources = state.data.resources.filter((item) => item.project_id === state.projectId);
  const memories = state.data.memories.filter((item) => item.project_id === state.projectId);
  const contextItem = (symbol, label, content) => el("div", { class: "context-item" }, icon(symbol), el("div", {}, el("div", { class: "context-label" }, label), el("div", { class: "context-value" }, content)));
  return el("aside", { class: "context-rail" },
    el("section", { class: "context-panel" }, el("div", { class: "section-heading" }, el("h2", {}, t("项目上下文", "Project context")), el("button", { type: "button", class: "text-button", "data-view": "resources" }, t("查看", "View"))),
      contextItem("folder", t("工作目录", "Project folder"), el("code", {}, current.path)),
      contextItem("document", t("共享资料", "Shared resources"), t(`${resources.length} 份资料`, `${resources.length} resources`)),
      contextItem("memory", t("项目记忆", "Project memory"), t(`${memories.length} 条记录`, `${memories.length} records`))),
    el("section", { class: "context-panel" }, el("div", { class: "section-heading" }, el("h2", {}, t("项目员工", "Project employees")), el("button", { type: "button", class: "text-button", "data-action": "discover" }, t("加入", "Add"))),
      people.length ? people.slice(0, 5).map((employee) => el("div", { class: "employee-mini" }, avatar(employee.name), el("div", {}, el("div", { class: "employee-name" }, employee.name), el("div", { class: "employee-kind" }, employee.kind)), tag(employee.lifecycle && employee.lifecycle !== "active" ? employee.lifecycle : employee.status))) : el("p", { class: "panel-note" }, t("先发现已有的 AI 工具，把员工加入这个项目。", "Discover existing AI tools and connect employees to this project."))));
}
function renderOverview() {
  if (!project()) return renderWelcome();
  const current = project();
  const tasks = projectTasks().slice().sort((a, b) => String(b.updated_at || b.created_at).localeCompare(String(a.updated_at || a.created_at)));
  const people = assignableEmployees();
  const finished = tasks.filter((task) => task.status === "done");
  const running = tasks.filter((task) => ["queued", "starting", "running"].includes(task.status));
  const reviewing = tasks.filter((task) => task.status === "review");
  const blocked = tasks.filter((task) => attentionStatuses.has(task.status));
  const firstRun = tasks.length === 0;
  return [heading(current.name, t("把目标、进度和成果放在同一个项目里。", "Keep goals, progress, and outcomes in one project."), [button(t("新任务", "New task"), "new-task", { class: "primary", icon: "plus", disabled: !state.data })]), runtimeNotice(),
    firstRun ? el("div", { class: "overview-layout" }, el("div", {}, el("section", { class: "onboarding-panel" },
      el("div", { class: "onboarding-step" }, el("span", { class: "step-number complete", "aria-hidden": "true" }, icon("check")), el("div", { class: "step-body" }, el("h2", {}, t("项目已准备好", "Your project is ready")), el("p", {}, el("code", {}, current.path)))),
      el("div", { class: "onboarding-step" }, el("span", { class: `step-number ${people.length ? "complete" : "active"}`, "aria-hidden": "true" }, people.length ? icon("check") : "2"), el("div", { class: "step-body" }, el("h2", {}, people.length ? t("在岗员工已加入项目", "Active employees are connected") : t("准备项目员工", "Prepare project employees")), el("p", {}, people.length ? t(`${people.length} 位在岗员工已加入，可以分配第一件工作。`, `${people.length} active employees are connected. Assign their first task.`) : projectEmployees().length ? t("当前成员没有可自动执行的在岗员工。可加入支持任务执行的 CLI，或恢复已暂停员工。", "No active members support automatic execution. Add a supported CLI employee or resume assignments.") : t("从全局登记的员工中选择本项目成员。", "Choose project members from your global employee registry.")), !people.length ? button(projectEmployees().length ? t("管理员工", "Manage employees") : t("选择已有员工", "Choose existing employees"), projectEmployees().length ? "new-task" : "add-member", { class: "primary", icon: "users" }) : null)),
      el("div", { class: "onboarding-step" }, el("span", { class: `step-number${people.length ? " active" : ""}`, "aria-hidden": "true" }, "3"), el("div", { class: "step-body" }, el("h2", {}, t("交代第一件事", "Assign the first task")), el("p", {}, t("写清目标与验收标准，员工的执行记录会出现在任务里。", "Describe the goal and acceptance criteria. The task will record the employee's work.")), button(t("创建第一个任务", "Create your first task"), "new-task", { class: people.length ? "primary" : "", icon: "plus", disabled: !people.length })))),
      el("p", { class: "welcome-footnote" }, t("默认只读资料。需要修改项目文件时，你可以在创建任务时选择。", "Tasks start read-only. Choose project write access when a task needs to edit files."))), projectContext()) :
    el("div", { class: "overview-layout" }, el("div", {},
      taskSection(t("正在做", "In progress"), running, t("当前没有排队或执行中的任务。", "No tasks are queued or running."), "play"),
      taskSection(t("等你验收", "Ready for your review"), reviewing, t("员工完成后，成果会在这里等待验收。", "Completed work will appear here for your review."), "review"),
      taskSection(t("需要你处理", "Needs your attention"), blocked, t("没有等待授权、失败或中断的任务。", "No tasks need permission or recovery."), "warning"),
      el("section", { class: "work-section" }, el("div", { class: "section-heading" }, el("h2", { class: "section-title" }, icon("check"), t("近期成果", "Recent outcomes")), el("button", { class: "text-button", type: "button", "data-view": "tasks" }, t("全部任务", "All tasks"))),
        finished.length ? el("ul", { class: "outcomes" }, finished.slice(0, 5).map((task) => el("li", { class: "outcome-row" }, icon("check"), el("div", {}, el("button", { type: "button", class: "text-button", "data-task": task.id }, task.title), el("p", {}, task.result || t("任务已通过验收。", "This task was accepted.")), el("div", { class: "task-meta" }, employeeName(task.assignee_id), formatDate(task.updated_at)))))) : el("p", { class: "inline-empty" }, t("通过验收的成果会留在这里，方便以后查阅。", "Accepted outcomes stay here for later reference.")))), projectContext())];
}
function renderTasks() {
  if (!project()) return [heading(navLabels.tasks, t("先选一个项目，再查看和分配任务。", "Select a project to view and assign tasks.")), empty(t("从一个项目开始", "Start with a project"), t("任务会保留目标、员工和完整工作记录。", "Tasks keep the goal, employee, and work record together."), "new-project", t("选择项目", "Choose project"), "task")];
  const filters = [["all", t("全部", "All")], ["active", t("进行中", "In progress")], ["review", t("待验收", "For review")], ["attention", t("需处理", "Needs attention")], ["done", t("已验收", "Accepted")]];
  const filterRow = el("div", { class: "toolbar" }, filters.map(([value, label]) => el("button", { type: "button", class: `filter-button${state.taskFilter === value ? " selected" : ""}`, "data-filter": value, "aria-pressed": String(state.taskFilter === value) }, label)));
  const search = el("input", { type: "search", class: "search-field", placeholder: t("搜索任务", "Search tasks"), "aria-label": t("搜索当前项目的任务", "Search this project's tasks"), value: state.search });
  search.addEventListener("input", () => { state.search = search.value; renderTaskList(); });
  filterRow.append(search);
  const list = el("div", { id: "task-list-container" });
  const result = [heading(navLabels.tasks, t("从交代目标到验收成果，每件工作都有记录。", "Every task records the path from goal to accepted outcome."), [button(t("新任务", "New task"), "new-task", { class: "primary", icon: "plus" })]), runtimeNotice(), filterRow, list];
  window.queueMicrotask(renderTaskList);
  return result;
}
function renderTaskList() {
  const container = document.getElementById("task-list-container");
  if (!container) return;
  const query = state.search.trim().toLocaleLowerCase();
  const tasks = projectTasks().filter((task) => {
    const filter = state.taskFilter;
    if (filter === "active" && !liveStatuses.has(task.status)) return false;
    if (filter === "attention" && !attentionStatuses.has(task.status)) return false;
    if (["review", "done"].includes(filter) && task.status !== filter) return false;
    return !query || [task.title, task.prompt, employeeName(task.assignee_id)].some((value) => String(value || "").toLocaleLowerCase().includes(query));
  }).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  if (tasks.length) container.replaceChildren(el("div", { class: "task-list" }, tasks.map(taskRow)));
  else if (projectTasks().length) container.replaceChildren(empty(t("没有匹配的任务", "No matching tasks"), t("试试其他状态或关键词。", "Try another status or search term."), null, null, "search"));
  else container.replaceChildren(empty(t("交代第一件事", "Assign the first task"), assignableEmployees().length ? t("描述目标和验收标准，选择员工，接下来的进度会自动记录。", "Describe the goal and acceptance criteria, then choose an employee. Progress will be recorded.") : t("先加入在岗员工，或恢复已暂停员工，再分配工作。", "Connect an active employee or resume a paused employee before assigning work."), "new-task", assignableEmployees().length ? t("创建任务", "Create task") : t("准备项目员工", "Prepare employees"), "task"));
}
function employeeRow(employee, { membership = false } = {}) {
  const lifecycle = employee.lifecycle || "active";
  const controls = el("div", { class: "employee-actions" });
  const check = button(t("检查连接", "Check connection"), null, { class: "small", icon: "refresh", attrs: { "aria-label": t(`检查 ${employee.name} 的连接`, `Check connection for ${employee.name}`) } });
  check.addEventListener("click", async () => {
    check.disabled = true;
    try { await api.checkEmployee(employee.id); await refresh(); showToast(t("连接与登录状态已重新检查；执行验证保持原记录。", "Connection and sign-in were rechecked; execution verification keeps its existing record.")); }
    catch (error) { showToast(errorText(error), true); check.disabled = false; }
  });
  controls.append(check);
  if (lifecycle !== "retired") {
    const change = button(lifecycle === "paused" ? t("恢复接任务", "Resume assignments") : t("暂停接任务", "Pause assignments"), null, { class: "small" });
    change.addEventListener("click", () => openLifecycleForm(employee, lifecycle === "paused" ? "active" : "paused"));
    const retire = button(t("退役员工", "Retire employee"), null, { class: "small employee-retire" });
    retire.addEventListener("click", () => openLifecycleForm(employee, "retired"));
    controls.append(change, retire);
  }
  if (membership) {
    const remove = button(t("移出项目", "Remove from project"), null, { class: "small" });
    remove.addEventListener("click", async () => {
      const projectId = state.projectId;
      remove.disabled = true;
      try { await api.removeProjectMember(projectId, employee.id); await refresh(); showToast(t("已移出项目，员工身份与历史仍保留。", "Removed from this project. Identity and history are retained.")); }
      catch (error) { showToast(errorText(error), true); remove.disabled = false; }
    });
    controls.append(remove);
  }
  const connection = employee.connection_type === "app" ? t("桌面应用", "Desktop app") : t("命令行 CLI", "CLI");
  const verified = employee.execution_verified === true || employee.verified === true;
  const facts = el("dl", { class: "employee-facts" },
    el("div", {}, el("dt", {}, t("设备", "Device")), el("dd", {}, deviceName(employee.node_id))),
    el("div", {}, el("dt", {}, t("触发方式", "Trigger")), el("dd", {}, employee.execution_supported ? t("受管任务会话", "Managed task session") : t("仅登记，自动执行暂不支持", "Registered; automatic execution unsupported"))),
    el("div", {}, el("dt", {}, t("原生登录", "Native sign-in")), el("dd", {}, tag(employee.auth_status || "unknown"))),
    el("div", {}, el("dt", {}, t("执行验证", "Execution check")), el("dd", {}, verified ? t("已验证", "Verified") : t("尚未验证", "Not verified"))));
  const row = el("article", { class: "employee-row employee-registry-row", "data-employee-id": employee.id, draggable: String(!membership && lifecycle !== "retired") }, avatar(employee.name),
    el("div", { class: "employee-profile" }, el("div", { class: "employee-title" }, el("h2", { class: "employee-name" }, employee.name), tag(lifecycle)),
      el("div", { class: "employee-kind" }, `${employee.kind} · ${connection}`), employee.entrypoint ? el("p", { class: "employee-detail" }, el("code", {}, employee.entrypoint)) : null,
      employee.detail ? el("p", { class: "employee-detail" }, humanDetail(employee.detail)) : null,
      employee.lifecycle_reason ? el("p", { class: "employee-reason" }, t("调整原因", "Reason"), " · ", employee.lifecycle_reason) : null,
      facts), controls);
  if (!membership && lifecycle !== "retired") {
    row.addEventListener("dragstart", (event) => {
      if (!state.data.employees.some((item) => item.id === employee.id && item.lifecycle !== "retired")) { event.preventDefault(); return; }
      state.dragEmployeeId = employee.id;
      event.dataTransfer.clearData(); event.dataTransfer.setData("text/plain", employee.id); event.dataTransfer.effectAllowed = "copy";
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => {
      state.dragEmployeeId = null; row.classList.remove("dragging");
      document.querySelectorAll(".drag-target").forEach((control) => control.classList.remove("drag-target"));
    });
  }
  return row;
}
function renderEmployees() {
  const people = state.data.employees;
  return [heading(navLabels.employees, t("先登记已有 AI 员工，再按项目组织协作。一个员工可以加入多个项目。", "Register existing AI employees, then organize them by project. An employee can join several projects."), [button(t("发现员工", "Discover employees"), "discover", { class: "primary", icon: "plus" })]),
    people.length ? el("div", { class: "employee-table" }, people.map((employee) => employeeRow(employee))) : empty(t("从你已有的员工开始", "Start with your existing employees"), t("检查这台设备上的 CLI 和桌面应用。现在就能登记员工，无需先创建项目。", "Discover CLI tools and desktop apps on this device. Register employees before creating a project."), "discover", t("发现已有员工", "Discover existing employees"), "users"),
    el("p", { class: "welcome-footnote" }, t("登录、自动执行支持与执行验证分别显示。Codex CLI 会启动受管任务会话，不会接管当前桌面对话。", "Sign-in, execution support, and verified execution are shown separately. Codex CLI starts a managed task session; it does not attach to this desktop conversation.")),
    el("p", { class: "welcome-footnote" }, t("可将员工拖到左侧项目；也可在项目成员页通过按钮选择。加入项目不会启动执行。", "Drag employees onto a project in the sidebar, or choose them with buttons on Project members. Joining does not start execution.")),
    button(t("创建项目并组织团队", "Create a project and organize your team"), "new-project", { icon: "folder" })];
}
function renderMembers() {
  if (!project()) return [heading(navLabels.members), empty(t("为团队选择一个项目", "Choose a project for your team"), t("员工已在全局登记；创建项目后选择需要的成员。", "Employees are registered globally. Create a project and choose its members."), "new-project", t("创建项目", "Create project"), "users")];
  const people = projectEmployees();
  return [heading(navLabels.members, t("从已登记员工中选择成员。移出项目不会删除员工身份或历史。", "Choose from registered employees. Removing a member keeps their identity and history."), [button(t("选择已有员工", "Choose existing employees"), "add-member", { class: "primary", icon: "plus" }), button(t("发现更多员工", "Discover more employees"), "discover", { icon: "search" })]),
    people.length ? el("div", { class: "employee-table" }, people.map((employee) => employeeRow(employee, { membership: true }))) : empty(t("把已有员工加入团队", "Add existing employees to your team"), t("选择登记过的员工加入此项目，然后通过消息沟通或派发工作任务。", "Choose registered employees for this project, then send messages or assign work."), "add-member", t("选择已有员工", "Choose existing employees"), "users"),
    renderGovernancePanel()];
}
function openMemberForm() {
  if (!project()) { openProjectForm(); return; }
  const projectId = state.projectId;
  const members = new Set(projectEmployees().map((item) => item.id));
  const people = state.data.employees.filter((item) => item.lifecycle !== "retired" && !members.has(item.id));
  const body = openForm(t("选择已有员工", "Choose existing employees"), t("加入项目只建立成员关系，不会启动员工或执行任务。", "Joining creates project membership without starting employees or executing tasks."));
  if (!body) return;
  if (!people.length) { body.append(empty(t("暂无可加入的员工", "No employees available to add"), t("所有在册员工已在项目中，或尚未登记员工。可先发现更多员工。", "All eligible employees are already members, or none are registered. Discover more employees first."), "discover", t("发现员工", "Discover employees"), "users")); return; }
  const box = errorBox();
  const list = el("div", { class: "member-picker" });
  people.forEach((employee) => {
    const add = button(t("加入项目", "Add to project"), null, { class: "small primary" });
    add.addEventListener("click", () => submitAction(add, box, () => api.addProjectMember(projectId, employee.id), async () => {
      await refresh(); add.textContent = t("已加入", "Added"); add.disabled = true;
      add.remove(); showToast(t(`${employee.name} 已加入项目。`, `${employee.name} joined the project.`));
    }));
    list.append(el("div", { class: "member-picker-row" }, avatar(employee.name), el("div", {}, el("strong", {}, employee.name), el("p", { class: "muted small-text" }, employee.kind, " · ", employee.connection_type === "app" ? t("桌面应用", "Desktop app") : "CLI", employee.execution_supported ? "" : t(" · 暂不支持自动执行", " · Automatic execution unsupported"))), add));
  });
  body.append(list, box);
}
async function loadMessages() {
  if (!project() || state.view !== "messages" || state.messagesLoading) return;
  const projectId = state.projectId;
  state.messagesLoading = true; state.messagesError = null;
  try {
    const result = await api.projectMessages(projectId);
    const messages = Array.isArray(result) ? result : result.messages;
    if (!Array.isArray(messages)) throw new ApiError("invalid_response");
    state.messages[projectId] = messages;
  } catch (error) { if (state.projectId === projectId) state.messagesError = error; }
  finally {
    state.messagesLoading = false;
    if (state.view === "messages" && state.projectId === projectId && !root.contains(document.activeElement)) render();
  }
}
function renderMessages() {
  if (!project()) return [heading(navLabels.messages), empty(t("选择消息所属的项目", "Choose a project for messages"), t("项目消息与成员、资料和工作记录一起保存。", "Project messages stay with members, resources, and work records."), "new-project", t("创建项目", "Create project"))];
  const projectId = state.projectId;
  const messages = state.messages[projectId] || (state.data.messages || []).filter((item) => item.project_id === projectId);
  const draft = state.messageDrafts[projectId] ||= { title: "", body: "", recipientId: "", replyTo: null, requestWork: false, requestId: null };
  const thread = el("section", { class: "message-list", "aria-label": t("项目消息记录", "Project message history") });
  if (state.messagesError) thread.append(el("p", { class: "form-error", role: "alert" }, errorText(state.messagesError)));
  if (!messages.length) thread.append(el("div", { class: "inline-empty" }, el("h2", {}, t("发出第一条项目消息", "Send the first project message")), el("p", {}, t("消息用于沟通和共享背景。需要员工动手时，明确选择“请求协作”。", "Use messages to share context. Choose Request collaboration when an employee should perform work."))));
  messages.forEach((message) => {
    const sender = message.sender?.name || (message.sender_id ? employeeName(message.sender_id) : t("你 · Human", "You · Human"));
    const recipient = message.recipient?.name || (message.recipient_id ? employeeName(message.recipient_id) : t("项目全员", "Everyone in this project"));
    const attribution = message.sender_id ? el("p", { class: "message-attribution" },
      t("登记员工身份", "Registered employee identity"), " · ", message.sender_session_id || message.session_id ? `${t("会话", "Session")} ${message.sender_session_id || message.session_id}` : t("会话编号未提供", "Session ID not provided"),
      message.internal_actor_verified === true ? "" : t(" · 内部子代理来源未独立核实", " · Internal subagent attribution is not independently verified")) : null;
    const reply = button(t("回复", "Reply"), null, { class: "small" });
    reply.addEventListener("click", () => {
      draft.replyTo = message.id; draft.requestWork = false; draft.requestId = null;
      draft.title = message.title ? `${t("回复", "Re")}：${message.title}`.slice(0, 240) : t("回复消息", "Reply to message");
      draft.recipientId = message.sender_id && projectEmployees().some((item) => item.id === message.sender_id) ? message.sender_id : "";
      render(); document.getElementById("message-body")?.focus();
    });
    const work = message.task_id ? button(t("查看关联任务", "Open linked task"), null, { class: "small", attrs: { "data-task": message.task_id } }) : null;
    thread.append(el("article", { class: "message-row", "data-message-id": message.id },
      el("div", { class: "message-meta" }, el("strong", {}, sender), el("span", {}, "→ ", recipient), el("time", {}, formatDate(message.created_at, true))),
      el("h2", {}, message.title), message.reply_to ? el("p", { class: "message-attribution" }, t("回复消息", "Reply to message"), " · ", messages.find((item) => item.id === message.reply_to)?.title || t("较早的项目消息", "Earlier project message")) : null,
      el("p", { class: "message-body" }, message.body), attribution,
      el("div", { class: "message-actions" }, reply, work, message.request_work ? el("span", { class: "status-tag" }, t("已请求只读协作", "Read-only collaboration requested")) : null)));
  });
  const title = el("input", { id: "message-title", required: true, maxlength: 240, value: draft.title, placeholder: t("这条消息关于什么？", "What is this message about?") });
  const body = el("textarea", { id: "message-body", required: true, maxlength: 20000, value: draft.body, placeholder: t("共享背景、提出问题或补充资料…", "Share context, ask a question, or add information…") });
  const recipient = el("select", { id: "message-recipient" });
  const requestWork = el("input", { id: "message-request-work", type: "checkbox", checked: draft.requestWork });
  const populateRecipients = () => {
    const people = requestWork.checked ? assignableEmployees() : projectEmployees();
    recipient.replaceChildren(el("option", { value: "" }, requestWork.checked ? t("选择执行员工", "Choose an executing employee") : t("项目全员", "Everyone in this project")), ...people.map((employee) => el("option", { value: employee.id }, employee.name)));
    recipient.value = people.some((item) => item.id === draft.recipientId) ? draft.recipientId : "";
    recipient.required = requestWork.checked;
  };
  populateRecipients();
  const error = errorBox();
  const send = el("button", { type: "submit", class: "button primary" }, t("发送消息", "Send message"));
  const explanation = el("p", { class: "field-hint" }, t("普通消息只保存沟通记录，不会启动员工。请求协作会创建关联的只读任务；执行与验收状态在任务中显示。", "Ordinary messages record communication without starting employees. Collaboration creates a linked read-only task; execution and review appear in that task."));
  const capture = () => {
    draft.title = title.value; draft.body = body.value; draft.recipientId = recipient.value; draft.requestWork = requestWork.checked; draft.requestId = null;
    send.textContent = requestWork.checked ? t("发送并请求协作", "Send and request collaboration") : t("发送消息", "Send message");
  };
  title.addEventListener("input", capture); body.addEventListener("input", capture); recipient.addEventListener("change", capture);
  requestWork.addEventListener("change", () => { populateRecipients(); capture(); });
  const form = el("form", { class: "message-composer" }, el("h2", {}, draft.replyTo ? t("回复项目消息", "Reply to a project message") : t("写一条消息", "Write a message")),
    formField(t("标题", "Title"), title), formField(t("收件人", "Recipient"), recipient), formField(t("内容", "Message"), body),
    el("label", { class: "checkbox-choice" }, requestWork, el("span", {}, t("请求协作（创建只读工作任务）", "Request collaboration (creates a read-only task)"))), explanation, error,
    el("div", { class: "message-actions" }, send));
  if (draft.replyTo) {
    const cancelReply = button(t("取消回复", "Cancel reply"), null, { class: "small" });
    cancelReply.addEventListener("click", () => { draft.replyTo = null; draft.requestId = null; render(); });
    form.prepend(el("div", { class: "message-reply-context" }, el("span", {}, t("回复消息", "Reply to message"), " · ", draft.replyTo), cancelReply));
  }
  send.textContent = draft.requestWork ? t("发送并请求协作", "Send and request collaboration") : t("发送消息", "Send message");
  form.addEventListener("submit", (event) => {
    event.preventDefault(); if (!title.value.trim() || !body.value.trim() || (requestWork.checked && !recipient.value)) return;
    const payload = { title: title.value.trim(), body: body.value.trim(), recipient_id: recipient.value || null, reply_to: draft.replyTo, request_work: requestWork.checked, request_id: draft.requestId ||= crypto.randomUUID() };
    submitAction(send, error, () => api.sendProjectMessage(projectId, payload), async (result) => {
      state.messageDrafts[projectId] = { title: "", body: "", recipientId: "", replyTo: null, requestWork: false, requestId: null };
      delete state.messages[projectId]; await refresh(); await loadMessages(); render();
      showToast(payload.request_work ? t("消息已发送，关联协作任务已创建。", "Message sent and collaboration task created.") : t("消息已发送。", "Message sent."));
    });
  });
  return [heading(navLabels.messages, t("在项目中沟通，再按需要请求工作。消息记录与执行任务分别保留。", "Communicate within the project and request work when needed. Messages and execution tasks retain separate records.")), el("div", { class: "messages-layout" }, thread, form)];
}
const governanceLabels = {
  task_dispatched: t("派发任务", "Task assigned"), employee_joined: t("员工加入", "Employee connected"),
  employee_lifecycle: t("员工状态调整", "Employee lifecycle changed"), permission_decided: t("操作授权决定", "Permission decided"), task_reviewed: t("成果验收", "Work reviewed"),
  permission_expired: t("授权请求过期", "Permission expired"),
};
function governanceActor(actor) {
  if (actor === "human") return t("人工操作", "Human action");
  if (actor === "runtime") return t("执行服务", "Execution service");
  if (typeof actor === "string" && actor.startsWith("employee:")) return t("员工", "Employee") + " · " + employeeName(actor.slice(9));
  if (typeof actor === "string" && actor.startsWith("device:")) return t("设备", "Device") + " · " + deviceName(actor.slice(7));
  return actor || t("来源未记录", "Actor not recorded");
}
function renderGovernancePanel() {
  const ids = new Set(projectEmployees().map((employee) => employee.id));
  const events = (state.governance || []).filter((event) => event.project_id === state.projectId || (!event.project_id && ids.has(event.employee_id))).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, 30);
  const retry = button(t("刷新记录", "Refresh records"), null, { class: "small", icon: "refresh", disabled: state.governanceLoading });
  retry.addEventListener("click", () => loadGovernance());
  return el("section", { id: "governance-panel", class: "governance-panel", "aria-busy": String(state.governanceLoading) },
    el("div", { class: "section-heading" }, el("h2", {}, t("管理记录", "Management history")), retry),
    state.governanceError ? el("p", { class: "form-error", role: "alert" }, errorText(state.governanceError)) : null,
    state.governance === null ? el("p", { class: "inline-empty" }, state.governanceLoading ? t("正在读取管理记录…", "Loading management history…") : t("管理记录尚未读取。", "Management history has not loaded.")) :
      events.length ? el("ol", { class: "governance-list" }, events.map((event) => el("li", { class: "governance-row" }, el("div", {}, el("p", { class: "governance-title" }, governanceLabels[event.type] || t("管理操作", "Management action"), event.employee_id ? " · " + employeeName(event.employee_id) : ""), event.reason ? el("p", { class: "governance-reason" }, event.reason) : null, el("p", { class: "governance-actor" }, governanceActor(event.actor))), el("time", { datetime: event.created_at || "" }, formatDate(event.created_at, true))))) : el("p", { class: "inline-empty" }, t("这个项目还没有管理记录。", "No management history for this project yet.")));
}
async function loadGovernance() {
  if (state.applicationStopping || state.applicationStopped) return;
  if (!state.projectId || state.view !== "members") return;
  if (state.governanceLoading) { state.governanceQueued = true; return; }
  state.governanceLoading = true;
  state.governanceError = null;
  const projectId = state.projectId;
  const update = () => { if (state.view === "members") document.getElementById("governance-panel")?.replaceWith(renderGovernancePanel()); };
  update();
  try {
    const result = await api.governance(projectId);
    if (!Array.isArray(result.events)) throw new ApiError("invalid_response");
    if (projectId === state.projectId) state.governance = result.events;
    else state.governanceQueued = true;
  } catch (error) { if (projectId === state.projectId) state.governanceError = error; }
  finally {
    state.governanceLoading = false;
    update();
    if (state.governanceQueued) { state.governanceQueued = false; loadGovernance(); }
  }
}
function memoryRow(memory) {
  const remove = el("button", { type: "button", class: "icon-button", "data-delete-memory": memory.id, "aria-label": t(`删除记忆：${memory.title}`, `Delete memory: ${memory.title}`), title: t("删除记忆", "Delete memory") }, icon("trash"));
  const source = memory.source === "human" ? t("由你记录", "Recorded by you") : memory.source || t("手动记录", "Manual record");
  return el("article", { class: "memory-row" }, el("header", {}, el("h3", {}, memory.title), remove), el("p", { class: "memory-body" }, memory.body), el("div", { class: "memory-meta" }, el("span", {}, source), el("span", {}, formatDate(memory.updated_at || memory.created_at))));
}
function renderResources() {
  if (!project()) return [heading(navLabels.resources, t("共享背景、决定和资料，让员工接着已有工作继续。", "Share context, decisions, and resources so employees can continue the work.")), empty(t("先选择一个项目", "Choose a project first"), t("资料和记忆按项目保存。", "Resources and memory are saved by project."), "new-project", t("选择项目", "Choose project"))];
  const resources = state.data.resources.filter((item) => item.project_id === state.projectId);
  const memories = state.data.memories.filter((item) => item.project_id === state.projectId);
  const memoryList = el("div", { id: "memory-list" }, memories.length ? memories.map(memoryRow) : el("p", { class: "inline-empty" }, t("记下已经确认的决定、约束或经验，方便后续员工继续工作。", "Record confirmed decisions, constraints, and lessons for future work.")));
  const search = el("input", { type: "search", placeholder: t("搜索项目记忆", "Search project memory"), "aria-label": t("搜索项目记忆", "Search project memory") });
  let timer;
  let sequence = 0;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    const query = search.value.trim();
    const requestId = ++sequence;
    const searchedProjectId = state.projectId;
    if (!query) { memoryList.replaceChildren(...(memories.length ? memories.map(memoryRow) : [el("p", { class: "inline-empty" }, t("还没有项目记忆。", "No project memory yet."))])); return; }
    timer = window.setTimeout(async () => {
      try {
        const result = await api.searchMemory(searchedProjectId, query);
        if (requestId !== sequence || state.projectId !== searchedProjectId || !memoryList.isConnected) return;
        memoryList.replaceChildren(...(result.memories?.length ? result.memories.map(memoryRow) : [el("p", { class: "inline-empty" }, t("没有匹配的记忆。", "No matching memory."))]));
      } catch (error) { if (requestId === sequence && memoryList.isConnected) showToast(errorText(error), true); }
    }, 300);
  });
  return [heading(navLabels.resources, t("让背景资料与已确认的决定，成为下一次工作的起点。", "Make context and confirmed decisions the starting point for the next task.")),
    el("div", { class: "resource-layout" }, el("section", {}, el("div", { class: "section-heading" }, el("h2", {}, t("共享资料", "Shared resources")), button(t("添加资料", "Add resource"), "add-resource", { class: "small", icon: "plus" })),
      resources.length ? resources.map((resource) => el("article", { class: "resource-row" }, icon("document"), el("div", {}, el("h3", {}, resource.name), el("code", {}, resource.path), el("p", { class: "resource-kind" }, resource.kind)))) : el("p", { class: "inline-empty" }, t("添加需求、规则或参考资料的文本文件，供员工在任务中读取。", "Add text files with requirements, rules, or references for employees to read during tasks."))),
      el("section", {}, el("div", { class: "section-heading" }, el("h2", {}, t("项目记忆", "Project memory")), button(t("记录记忆", "Add memory"), "add-memory", { class: "small", icon: "plus" })), el("div", { class: "toolbar" }, search), memoryList))];
}
function renderDevices() {
  const devices = state.data?.devices || [];
  const fleet = state.data.fleet;
  const remote = fleet?.remote;
  const shared = remote?.projects || [];
  const mappings = remote?.mappings || {};
  const errors = remote?.worker?.errors || {};
  const pairCards = el("div", { class: "fleet-options" },
    el("section", { class: "fleet-card" }, el("span", { class: "eyebrow" }, t("在主控设备上", "On the coordinator")), el("h2", {}, t("邀请另一台设备", "Invite another device")), el("p", {}, t("按需启用局域网或私网接入，选择共享项目，生成一次性邀请。在这里统一派单和验收。", "Optionally enable local or private network connections, select projects, and create a single-use invitation. Assign and review work here.")),
      fleet?.listener ? el("div", { class: "fleet-actions" }, button(t("生成设备邀请", "Create device invitation"), "fleet-invite", { class: "primary small", disabled: !state.data.projects.length }), button(t("暂停设备接入", "Pause device connections"), "fleet-stop", { class: "small" })) : button(t("启用设备接入", "Enable device connections"), "fleet-start", { class: "primary small", icon: "link" }),
      fleet?.listener ? el("p", { class: "fleet-address" }, t("接入地址", "Connection address"), " · ", el("code", {}, fleet.listener.base_url)) : null),
    el("section", { class: "fleet-card" }, el("span", { class: "eyebrow" }, t("在另一台设备上", "On the other device")), el("h2", {}, remote?.paired ? t("已加入主控", "Connected to coordinator") : t("加入已有团队", "Join an existing team")), el("p", {}, remote?.paired ? t("为共享项目选择这台设备的工作目录，再加入已安装的员工。主控就能把任务交给它。", "Choose this device's working folder for a shared project, then connect an installed employee. The coordinator can assign work to it.") : t("在另一台设备打开工作台，粘贴主控生成的邀请。项目文件需要提前准备在这台设备上。", "Open the workbench on the other device and paste the coordinator's invitation. Prepare the project files on that device first.")),
      remote?.paired ? el("div", { class: "fleet-actions" }, el("span", { class: "status-tag online" }, t("设备已配对", "Device paired")), button(t("断开此设备", "Disconnect this device"), "fleet-leave", { class: "small" })) : button(t("粘贴邀请并加入", "Paste invitation to join"), "fleet-join", { class: "small", icon: "link" })));
  const remoteProjects = shared.length ? el("section", { class: "remote-projects" }, el("div", { class: "section-heading" }, el("h2", {}, t("这台设备参与的共享项目", "Shared projects on this device"))), shared.map((item) => {
    const mapped = mappings[item.id];
    const mapButton = button(mapped ? t("更换本机目录", "Change local folder") : t("选择本机目录", "Choose local folder"), null, { class: "small", icon: "folder" });
    mapButton.addEventListener("click", () => openRemoteMapForm(item));
    const add = button(t("加入这台设备的员工", "Connect an employee on this device"), null, { class: "small", disabled: !mapped });
    add.addEventListener("click", () => openRemoteEmployeeForm(item));
    return el("article", { class: "remote-project-row" }, el("div", {}, el("h3", {}, item.name), mapped ? el("code", {}, mapped) : el("p", { class: "muted" }, t("尚未选择工作目录；不会领取任务。", "No working folder selected. Tasks will not be claimed.")), errors[item.id] ? el("p", { class: "fleet-error", role: "status" }, errors[item.id]) : null), el("div", { class: "fleet-actions" }, mapButton, add));
  })) : null;
  const rows = devices.map((device) => {
    const local = state.data.node?.id === device.id;
    const revoke = !local && fleet?.listener && !device.revoked ? button(t("撤销配对", "Revoke pairing"), null, { class: "small" }) : null;
    revoke?.addEventListener("click", () => confirmFleetAction("revoke", device));
    return el("div", { class: "device-row" }, el("span", { class: "device-icon", "aria-hidden": "true" }, icon("monitor")), el("div", { class: "device-row-main" }, el("h2", {}, device.name), el("p", {}, local ? t("当前工作台所在设备", "This workbench's device") : t("已登记设备", "Registered device")), device.last_seen ? el("p", {}, `${t("最近连接", "Last seen")} · ${formatDate(device.last_seen, true)}`) : null), tag(device.revoked ? "revoked" : device.status), revoke);
  });
  return [heading(navLabels.devices, t("单机使用无需配置设备。需要更多执行节点时，可连接局域网或私网中的设备；远处设备需先通过私网或隧道可达。", "Single-computer use needs no device setup. Add execution nodes on a local or private network when needed; distant devices must first be reachable through a private network or tunnel.")),
    fleet?.error ? el("div", { class: "runtime-notice error", role: "status" }, icon("warning"), el("span", {}, fleet.error.message || t("设备连接需要重新检查。", "Check the device connection.")), button(t("重新检查", "Check again"), "refresh", { class: "small" })) : null,
    fleet === undefined ? el("p", { class: "device-info" }, t("当前服务未提供设备配对。请更新工作台服务后重试。", "This service does not provide device pairing. Update the workbench service and try again.")) : pairCards,
    remote ? runtimeNotice({ force: true }) : null,
    remoteProjects,
    errors.recovery ? el("p", { class: "fleet-error" }, errors.recovery) : null,
    el("section", { class: "device-list" }, el("div", { class: "section-heading" }, el("h2", {}, t("设备状态", "Device status"))), rows.length ? rows : el("p", { class: "inline-empty" }, t("还没有可显示的设备。", "No devices to display."))),
    el("p", { class: "device-info" }, t("项目目录会分别留在各台设备上，配对不会同步文件。每台设备使用自己的 AI 工具登录；任务结果、授权和验收回到主控。", "Project folders stay on each device. Pairing does not synchronize files. Each device uses its own AI tool sign-in; results, permissions, and review return to the coordinator."))];
}
function renderDisconnected() {
  return [heading(t("工作台暂未连接", "The workbench is disconnected"), t("连接服务后，你的项目、员工和任务会显示在这里。", "Your projects, employees, and tasks appear when the service is connected.")),
    empty(t("连接本机工作台", "Connect to your workbench"), errorText(state.error || new ApiError("connection_failed")), "refresh", t("重新连接", "Reconnect"), "link")];
}
function render() {
  renderSidebar();
  if (state.applicationStopped) {
    root.replaceChildren(heading(t("应用已退出", "Application closed"), t("任务、成果和项目记录仍然保留。", "Tasks, deliverables, and project records are retained.")), empty(t("重新启动 Agent Mailbox", "Restart Agent Mailbox"), t("可以关闭这个浏览器页面。使用原来的应用入口或启动命令重新启动，再打开它提供的新地址。", "You can close this browser page. Restart using your application launcher or original command, then open its new address."), null, null, "monitor"));
    root.setAttribute("aria-busy", "false");
    return;
  }
  const renders = { overview: renderOverview, tasks: renderTasks, employees: renderEmployees, members: renderMembers, messages: renderMessages, resources: renderResources, devices: renderDevices };
  const content = state.data ? renders[state.view]() : renderDisconnected();
  root.replaceChildren(...content.flat(Infinity).filter((item) => item !== null && item !== undefined && item !== false));
  root.setAttribute("aria-busy", "false");
}
async function refresh({ silent = false } = {}) {
  if (state.applicationStopping || state.applicationStopped) return;
  if (state.refreshing) return;
  state.refreshing = true;
  const control = document.getElementById("refresh-button");
  control.disabled = true;
  try {
    const result = await api.snapshot();
    if (state.applicationStopping || state.applicationStopped) return;
    for (const key of ["projects", "employees", "tasks", "resources", "memories", "devices"]) {
      if (!Array.isArray(result[key])) throw new ApiError("invalid_response");
    }
    state.data = result;
    if (Array.isArray(result.messages)) { for (const item of result.projects) state.messages[item.id] = result.messages.filter((message) => message.project_id === item.id); }
    document.getElementById("version-label").textContent = result.version ? `v${result.version.replace(/^v/, "")}` : "";
    state.error = null;
    if (!result.projects.some((item) => item.id === state.projectId)) state.projectId = result.projects[0]?.id || "";
    if (result.runtime?.installed || ["ready", "failed"].includes(result.runtime?.install?.status)) state.runtimeInstalling = false;
    state.taskFormUpdate?.();
    updateConnection(true);
    setNotice(null);
    const activeElement = document.activeElement;
    // Preserve ongoing searches and form input during background updates.
    if (!silent || !root.contains(activeElement) || !["INPUT", "TEXTAREA", "SELECT"].includes(activeElement?.tagName)) render();
    if (state.view === "members") loadGovernance();
  } catch (error) {
    if (state.applicationStopping || state.applicationStopped) return;
    state.error = error;
    updateConnection(false);
    setNotice(state.data ? `${t("更新失败，当前显示上次读取的记录。", "Update failed. The previous records are still displayed.")} ${errorText(error)}` : errorText(error), true);
    if (!state.data) render();
    if (!silent && state.data) showToast(errorText(error), true);
  } finally { state.refreshing = false; control.disabled = state.applicationStopped; }
}

function openForm(title, description) {
  if (!state.data) { showToast(errorText(state.error || new ApiError("connection_failed")), true); return null; }
  state.taskFormUpdate = null;
  formContent.replaceChildren();
  const close = el("button", { class: "icon-button", type: "button", "aria-label": t("关闭", "Close") }, icon("close"));
  close.addEventListener("click", () => formDialog.close());
  formContent.append(el("div", { class: "dialog-heading" }, el("h2", { id: "form-dialog-title" }, title), close));
  if (description) formContent.append(el("p", { class: "dialog-description" }, description));
  const body = el("div", { class: "form-body" });
  formContent.append(body);
  formDialog.showModal();
  return body;
}
function formField(label, input, hint) {
  return el("div", { class: "form-field" }, el("label", { for: input.id }, label), input, hint ? el("p", { class: "field-hint" }, hint) : null);
}
function errorBox() { return el("div", { class: "form-error", role: "alert", tabindex: "-1", hidden: true }); }
function formError(box, error) { box.textContent = errorText(error); box.hidden = false; box.focus(); }
async function submitAction(control, box, action, success) {
  control.disabled = true;
  control.dataset.loading = "true";
  box.hidden = true;
  try { const result = await action(); await success(result); }
  catch (error) { formError(box, error); }
  finally { control.disabled = false; delete control.dataset.loading; }
}
function footer(submitLabel) {
  const cancel = button(t("取消", "Cancel"), null);
  cancel.removeAttribute("data-action");
  cancel.addEventListener("click", () => formDialog.close());
  const submit = el("button", { class: "button primary", type: "submit" }, submitLabel);
  return { node: el("div", { class: "form-footer" }, cancel, submit), submit };
}
function openLifecycleForm(employee, lifecycle) {
  if (employee.lifecycle === "retired") return;
  const retiring = lifecycle === "retired";
  const label = retiring ? t("退役员工", "Retire employee") : lifecycle === "paused" ? t("暂停接任务", "Pause assignments") : t("恢复接任务", "Resume assignments");
  const description = retiring ? t(`确认是否退役“${employee.name}”。这会改变员工身份的权限与任务接收状态。`, `Confirm whether to retire “${employee.name}”. This changes the identity's access and assignment state.`) : lifecycle === "paused" ? t(`暂停“${employee.name}”接收新任务，已经执行的工作会继续。`, `Pause new assignments for “${employee.name}”. Work already running will continue.`) : t(`让“${employee.name}”重新接收任务，等待中的任务可以继续领取。`, `Let “${employee.name}” receive assignments again. Queued tasks can be claimed.`);
  const body = openForm(label, description);
  if (!body) return;
  const reason = el("textarea", { id: "employee-lifecycle-reason", required: retiring, maxlength: 1000, placeholder: retiring ? t("请说明退役原因，记录会保留。", "Explain why this employee is being retired. The reason will be retained.") : t("可选：记录这次调整的原因。", "Optional: record the reason for this change.") });
  const box = errorBox();
  const end = footer(retiring ? t("确认退役", "Confirm retirement") : label);
  const form = el("form", {});
  let confirmation;
  if (retiring) {
    form.append(el("div", { class: "retirement-summary" }, el("p", {}, t("退役后会发生什么", "What retirement changes")), el("ul", {},
      el("li", {}, t("停止接收新任务，取消排队任务，并请求停止正在执行的任务。", "Stop new assignments, cancel queued tasks, and request running tasks to stop.")),
      el("li", {}, t("撤销共享资料与团队协作权限。", "Revoke shared context and team collaboration access.")),
      el("li", {}, t("保留已有任务、交付成果和管理记录。", "Keep existing tasks, deliverables, and management history.")),
      el("li", {}, t("原身份不能直接恢复；再次加入需要建立新身份。", "This identity cannot be resumed. Rejoining requires a new identity.")))));
    confirmation = el("input", { type: "checkbox", id: "employee-retirement-confirm", required: true });
    form.append(formField(t("退役原因（必填）", "Retirement reason (required)"), reason), el("label", { class: "retirement-confirm", for: confirmation.id }, confirmation, el("span", {}, t("我理解以上影响，确认退役这名员工。", "I understand these effects and confirm this employee's retirement."))));
    end.submit.classList.add("danger-action");
    end.submit.disabled = true;
    const validate = () => { end.submit.disabled = !confirmation.checked || !reason.value.trim(); };
    confirmation.addEventListener("change", validate); reason.addEventListener("input", validate);
  } else form.append(formField(t("调整原因（可选）", "Reason (optional)"), reason));
  form.append(box, end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (retiring && (!reason.value.trim() || !confirmation.checked)) return;
    submitAction(end.submit, box, () => api.setEmployeeLifecycle(employee.id, { status: lifecycle, reason: reason.value.trim() }), async () => {
      formDialog.close();
      await refresh();
      showToast(retiring ? t("员工已退役，历史记录保留。", "Employee retired. History is retained.") : lifecycle === "paused" ? t("员工已暂停接新任务，正在执行的任务继续。", "New assignments are paused. Running work continues.") : t("员工已恢复接任务。", "Employee assignments resumed."));
    }).finally(() => { if (retiring && formDialog.open && form.isConnected) end.submit.disabled = !confirmation.checked || !reason.value.trim(); });
  });
  body.append(form); reason.focus();
}
function openQuitForm() {
  const body = openForm(t("退出应用", "Quit application"), t("关闭浏览器页面不会停止应用。确认退出这台设备上的 Agent Mailbox。", "Closing the browser does not stop the application. Confirm quitting Agent Mailbox on this device."));
  if (!body) return;
  const running = state.data.tasks.filter((task) => ["starting", "running", "waiting_approval"].includes(task.status)).length;
  const box = errorBox();
  const end = footer(t("确认退出应用", "Confirm quit"));
  body.append(el("div", { class: "quit-explanation" }, el("p", { class: "quit-description" }, t("正在执行的任务会请求停止，任务记录和尚未验收的成果保留。只停止本应用启动的执行器。", "Running tasks will be asked to stop. Task history and work awaiting review are retained. Only executors started by this application are stopped.")),
    running ? el("p", { class: "quit-running-note" }, t(`当前记录中有 ${running} 个任务正在启动、执行或等待授权。`, `${running} recorded tasks are starting, running, or awaiting permission.`)) : null,
    el("p", { class: "field-hint" }, t("退出后，使用原来的应用入口或启动命令重新启动。这个页面不会关闭其他 AI 会话或浏览器标签页。", "Restart using your application launcher or original command. Other AI sessions and browser tabs remain open."))));
  const form = el("form", {}, box, end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    state.applicationStopping = true;
    submitAction(end.submit, box, () => api.quitApplication(), (result) => {
      if (result.stopping !== true) throw new ApiError("invalid_response");
      state.applicationStopped = true;
      state.applicationStopping = false;
      stopChanges();
      window.clearTimeout(state.changeTimer);
      window.clearTimeout(state.toastTimer);
      document.getElementById("toast-region").replaceChildren();
      state.data = null;
      setNotice(null);
      formDialog.close(); detailDialog.close();
      render();
      document.getElementById("service-dot").className = "status-dot";
      document.getElementById("service-label").textContent = t("应用已退出", "Application closed");
      document.getElementById("connection-label").replaceChildren(el("span", {}, t("已退出", "Closed")));
      for (const control of document.querySelectorAll("[data-view], [data-action], #refresh-button")) control.disabled = true;
      document.getElementById("main").focus({ preventScroll: true });
    }).finally(() => { if (!state.applicationStopped) state.applicationStopping = false; });
  });
  body.append(form);
}
function openFleetStartForm() {
  const body = openForm(t("启用设备接入（可选）", "Enable device connections (optional)"), t("单机使用无需开启。连接其他设备时，输入这台主控在局域网或私有网络中的 IPv4 地址；远处设备需先通过私网或隧道访问它。", "Single-computer use needs no setup here. Enter this coordinator's IPv4 address on a local or private network. Distant devices must first reach it through a private network or tunnel."));
  if (!body) return;
  const address = el("input", { id: "fleet-address", required: true, placeholder: "192.168.1.100", autocomplete: "off", spellcheck: "false" });
  const box = errorBox();
  const end = footer(t("启用设备接入", "Enable device connections"));
  const form = el("form", {}, formField(t("这台设备的私网地址", "This device's private network address"), address, t("可在系统网络设置中查看。仅支持私有 IPv4；公网直接接入未提供，工作台不会代你建立隧道。", "Find it in system network settings. Private IPv4 only. Direct public access is not provided, and the workbench does not create a tunnel for you.")), box, end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAction(end.submit, box, () => api.startFleet({ address: address.value.trim(), port: 0 }), async () => {
      formDialog.close(); await refresh(); showToast(t("设备接入已启用，接下来生成邀请。", "Device connections enabled. Create an invitation next."));
    });
  });
  body.append(form); address.focus();
}
function openFleetInviteForm() {
  const body = openForm(t("生成设备邀请", "Create device invitation"), t("选择允许另一台设备参与的项目。邀请只能使用一次，5 分钟内有效。", "Choose which projects the other device may join. The invitation can be used once within 5 minutes."));
  if (!body) return;
  const box = errorBox();
  const checks = state.data.projects.map((item, index) => {
    const input = el("input", { type: "checkbox", id: `invite-project-${index}`, value: item.id, checked: item.id === state.projectId });
    return { input, node: el("label", { class: "invite-project-choice", for: input.id }, input, el("span", {}, item.name)) };
  });
  const end = footer(t("生成邀请", "Create invitation"));
  const form = el("form", {}, el("fieldset", { class: "invite-project-list" }, el("legend", {}, t("允许参与的项目", "Allowed projects")), checks.map((item) => item.node)), box, end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const ids = checks.filter((item) => item.input.checked).map((item) => item.input.value);
    if (!ids.length) { formError(box, new ApiError(t("至少选择一个项目。", "Select at least one project."))); return; }
    submitAction(end.submit, box, () => api.inviteDevice(ids), (invite) => {
      const json = JSON.stringify(invite, null, 2);
      const value = el("textarea", { id: "fleet-invitation", class: "invitation-code", readonly: true, spellcheck: "false", value: json });
      const copy = button(t("复制邀请", "Copy invitation"), null, { class: "primary" });
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(json); showToast(t("邀请已复制。请在另一台设备粘贴。", "Invitation copied. Paste it on the other device.")); }
        catch { value.focus(); value.select(); showToast(t("请手动复制已选中的邀请内容。", "Copy the selected invitation manually.")); }
      });
      const done = button(t("完成", "Done"), null);
      done.addEventListener("click", () => formDialog.close());
      body.replaceChildren(el("p", { class: "invitation-notice" }, t("邀请包含接入凭据。只发给你希望加入的设备，不要公开发布。", "The invitation contains connection credentials. Share it only with the device you want to join.")), formField(t("设备邀请", "Device invitation"), value), el("p", { class: "field-hint" }, `${t("有效至", "Valid until")} · ${formatDate(new Date(invite.expires_at * 1000).toISOString(), true)}`), el("div", { class: "form-footer" }, done, copy));
    });
  });
  body.append(form);
}
function openFleetJoinForm() {
  const body = openForm(t("加入主控设备", "Join coordinator"), t("粘贴主控生成的完整邀请内容。请先确认邀请来自你希望加入的主控设备。", "Paste the complete invitation generated by the coordinator you intend to join."));
  if (!body) return;
  const value = el("textarea", { id: "fleet-join-invitation", required: true, spellcheck: "false", placeholder: t("在这里粘贴邀请内容", "Paste the invitation here") });
  const box = errorBox();
  const end = footer(t("确认邀请并加入", "Confirm invitation and join"));
  const form = el("form", {}, formField(t("设备邀请", "Device invitation"), value), box, end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    let invite;
    try {
      invite = JSON.parse(value.value);
      if (!invite || typeof invite !== "object" || Array.isArray(invite)) throw new Error();
    } catch { formError(box, new ApiError(t("邀请格式无法读取，请复制完整的邀请内容。", "The invitation format could not be read. Copy the complete invitation."))); return; }
    submitAction(end.submit, box, () => api.joinFleet(invite), async () => {
      value.value = ""; formDialog.close(); await refresh(); showToast(t("设备已配对，接下来选择本机项目目录。", "Device paired. Choose the local project folder next."));
    });
  });
  body.append(form); value.focus();
}
function openRemoteMapForm(sharedProject) {
  const body = openForm(t("选择这台设备的工作目录", "Choose this device's working folder"), t(`为“${sharedProject.name}”选择本机目录。请提前准备项目文件，配对不会复制文件。`, `Choose a local folder for “${sharedProject.name}”. Prepare the project files first; pairing does not copy them.`));
  if (!body) return;
  const path = el("input", { id: "fleet-project-path", required: true, value: state.data.fleet?.remote?.mappings?.[sharedProject.id] || "", placeholder: t("本机文件夹的绝对路径", "Absolute path to the local folder"), autocomplete: "off" });
  const pick = button(t("选择文件夹", "Choose folder"), null, { icon: "folder" });
  const box = errorBox();
  pick.addEventListener("click", async () => {
    pick.disabled = true;
    try { const result = await api.pickProject(); if (result.path) { path.value = result.path; box.hidden = true; } }
    catch (error) { formError(box, error); path.focus(); }
    finally { pick.disabled = false; }
  });
  const end = footer(t("使用这个目录", "Use this folder"));
  const form = el("form", {}, el("div", { class: "form-field" }, el("label", { for: path.id }, t("本机项目目录", "Local project folder")), el("div", { class: "field-row" }, path, pick), el("p", { class: "field-hint" }, t("选择器不可用时，直接输入已有目录路径。", "Enter an existing folder path if the picker is unavailable."))), box, end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAction(end.submit, box, () => api.mapRemoteProject({ project_id: sharedProject.id, path: path.value.trim() }), async () => { formDialog.close(); await refresh(); showToast(t("工作目录已设置，可以加入员工。", "Working folder set. Connect an employee next.")); });
  });
  body.append(form); path.focus();
}
async function openRemoteEmployeeForm(sharedProject) {
  const body = openForm(t("加入这台设备的员工", "Connect an employee on this device"), t(`加入“${sharedProject.name}”后，这名员工会出现在主控的项目员工列表中。`, `The employee will appear in the coordinator's employee list for “${sharedProject.name}”.`));
  if (!body) return;
  const name = el("input", { id: "fleet-employee-name", required: true, maxlength: 160, placeholder: t("例如：笔记本上的 Codex", "For example: Codex on laptop"), autocomplete: "off" });
  const kind = el("select", { id: "fleet-employee-kind", required: true, disabled: true }, el("option", { value: "" }, t("正在检查已有工具…", "Checking installed tools…")));
  const detail = el("p", { class: "field-hint" });
  const box = errorBox();
  const end = footer(t("加入项目", "Connect to project"));
  end.submit.disabled = true;
  const retry = button(t("重新检查", "Check again"), null, { class: "small", icon: "refresh" });
  const form = el("form", {}, formField(t("员工名称", "Employee name"), name), el("div", { class: "form-field" }, el("label", { for: kind.id }, t("本机已有工具", "Installed tool")), kind, detail), box, el("div", { class: "form-footer" }, retry, end.node));
  let employees = [];
  const updateDetail = () => { detail.textContent = humanDetail(employees.find((item) => item.kind === kind.value)?.detail || ""); };
  const discover = async () => {
    retry.disabled = true; end.submit.disabled = true; box.hidden = true;
    try {
      const result = await api.discoverEmployees();
      if (!formDialog.open || !body.isConnected) return;
      employees = result.employees || [];
      const supported = employees.filter((item) => ["codex", "claude"].includes(item.kind) && !["unavailable", "missing", "not_installed", "unsupported"].includes(item.status));
      kind.replaceChildren(...(supported.length ? supported.map((item) => el("option", { value: item.kind }, item.name)) : [el("option", { value: "" }, t("未发现可加入的工具", "No installed tool available"))]));
      kind.disabled = !supported.length; end.submit.disabled = !supported.length;
      updateDetail();
      if (!supported.length) detail.textContent = t("先安装并登录 Codex 或 Claude Code，再重新检查。", "Install and sign in to Codex or Claude Code, then check again.");
    } catch (error) { formError(box, error); }
    finally { retry.disabled = false; }
  };
  retry.addEventListener("click", discover); kind.addEventListener("change", updateDetail);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!kind.value) return;
    submitAction(end.submit, box, () => api.addRemoteEmployee({ project_id: sharedProject.id, name: name.value.trim(), kind: kind.value }), async () => { formDialog.close(); await refresh(); showToast(t("员工已加入。回到主控选择它来派发任务。", "Employee connected. Assign a task to it from the coordinator.")); });
  });
  body.append(form); name.focus(); await discover();
}
function confirmFleetAction(action, device) {
  const revoke = action === "revoke";
  const leave = action === "leave";
  const title = revoke ? t("撤销设备配对", "Revoke device pairing") : leave ? t("断开此设备", "Disconnect this device") : t("暂停设备接入", "Pause device connections");
  const description = revoke ? t(`撤销“${device.name}”的接入凭据。这台设备将无法继续访问共享项目。`, `Revoke the credentials for “${device.name}”. This device will no longer be able to access shared projects.`) : leave ? t("清除本机接入凭据，保留项目目录。主控仍保留设备信任；如需撤销信任，请在主控的设备页操作。", "Clear this device's connection credentials and keep project folders. The coordinator still trusts the device; revoke that trust from the coordinator's Devices page if needed.") : t("暂停主控的设备接入服务。其他设备暂时无法连接，你可以之后重新启用。", "Pause the coordinator's device connection service. Other devices cannot connect until you enable it again.");
  const body = openForm(title, description);
  if (!body) return;
  const box = errorBox(); const end = footer(title);
  const form = el("form", {}, box, end.node);
  form.addEventListener("submit", (event) => { event.preventDefault(); submitAction(end.submit, box, () => revoke ? api.revokeDevice(device.id) : leave ? api.leaveFleet() : api.stopFleet(), async () => { formDialog.close(); await refresh(); showToast(revoke ? t("设备配对已撤销。", "Device pairing revoked.") : leave ? t("此设备已断开，项目目录已保留。", "This device is disconnected. Project folders are kept.") : t("设备接入已暂停。", "Device connections paused.")); }); });
  body.append(form);
}
function openProjectForm() {
  const body = openForm(t("选择项目", "Choose a project"), t("选择工作目录。任务、员工和资料会围绕这个项目组织。", "Choose the working folder. Tasks, employees, and context will belong to this project."));
  if (!body) return;
  const name = el("input", { id: "project-name", name: "name", required: true, maxlength: 160, autocomplete: "off", placeholder: t("给项目起个名字", "Give the project a name") });
  const path = el("input", { id: "project-path", name: "path", required: true, autocomplete: "off", placeholder: t("输入项目文件夹的绝对路径", "Enter the absolute path to the project folder") });
  const pick = button(t("选择文件夹", "Choose folder"), null, { icon: "folder" });
  pick.removeAttribute("data-action");
  const box = errorBox();
  pick.addEventListener("click", async () => {
    pick.disabled = true;
    pick.dataset.loading = "true";
    try {
      const result = await api.pickProject();
      if (result.path) {
        path.value = result.path;
        if (!name.value.trim()) name.value = result.path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
        box.hidden = true;
      }
    } catch (error) {
      formError(box, error);
      path.focus();
    } finally { pick.disabled = false; delete pick.dataset.loading; }
  });
  const form = el("form", {}, formField(t("项目名称", "Project name"), name), el("div", { class: "form-field" }, el("label", { for: path.id }, t("项目文件夹", "Project folder")), el("div", { class: "field-row" }, path, pick), el("p", { class: "field-hint" }, t("无法打开选择器时，可以直接输入路径。已有目录和文件会保留。", "If the folder picker is unavailable, enter the path directly. Existing files are kept."))), box);
  const end = footer(t("创建项目", "Create project"));
  form.append(end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!name.value.trim() || !path.value.trim()) return;
    submitAction(end.submit, box, () => api.createProject({ name: name.value.trim(), path: path.value.trim() }), async (result) => {
      const created = result.project || result;
      state.projectId = created.id || state.projectId;
      try { sessionStorage.setItem("agent-mailbox.workbench.project", state.projectId); } catch { /* optional */ }
      state.view = "overview";
      formDialog.close();
      showToast(t("项目已创建，接下来加入员工。", "Project created. Connect employees next."));
      await refresh();
    });
  });
  body.append(form);
  name.focus();
}
async function openEmployeeForm() {
  const body = openForm(t("发现已有员工", "Discover existing employees"), t("登记本机已有的 CLI 或桌面应用。支持自动执行的 CLI 使用独立受管任务会话；桌面应用登记不会接管现有对话。", "Register CLI tools or desktop apps on this device. Supported CLIs use managed task sessions; app registration does not attach to an existing conversation."));
  if (!body) return;
  const list = el("div", { class: "discovery-list" });
  const box = errorBox();
  const retry = button(t("重新检查", "Check again"), null, { class: "small", icon: "refresh" });
  body.append(list, box, el("div", { class: "form-footer" }, retry));
  async function discover() {
    retry.disabled = true; box.hidden = true;
    list.replaceChildren(el("div", { class: "skeleton skeleton-row" }), el("p", { class: "muted small-text" }, t("正在检查已有工具…", "Checking installed tools…")));
    try {
      const result = await api.discoverEmployees();
      if (!formDialog.open || !list.isConnected) return;
      if (!Array.isArray(result.employees)) throw new ApiError("invalid_response");
      state.discovered = result.employees; list.replaceChildren();
      if (!result.employees.length) { list.append(el("p", { class: "inline-empty" }, t("没有发现已安装的员工工具。安装你要使用的 AI 工具后重新检查。", "No installed employee tools were found. Install an AI tool and check again."))); return; }
      result.employees.forEach((employee, index) => {
        const unavailable = ["missing", "not_installed", "unavailable"].includes(employee.status) || !employee.entrypoint;
        const connectionType = employee.connection_type || "cli";
        const identities = state.data.employees.filter((item) => item.kind === employee.kind && (item.connection_type || "cli") === connectionType && (!item.node_id || item.node_id === state.data.node?.id));
        const newName = el("input", { id: `new-employee-name-${index}`, maxlength: 160, autocomplete: "off", value: employee.name, placeholder: t("例如：代码审查员", "For example: Code reviewer"), "aria-label": t("员工名称", "Employee name") });
        const hint = el("p", { class: "field-hint" });
        const add = button(t("登记员工", "Register employee"), null, { class: "small primary" });
        const updateIdentity = () => {
          const existing = identities.find((item) => item.name === newName.value.trim());
          add.disabled = unavailable || !newName.value.trim() || Boolean(existing);
          add.textContent = existing?.lifecycle === "retired" ? t("身份已退役", "Identity retired") : existing ? t("已登记", "Registered") : t("登记员工", "Register employee");
          hint.textContent = existing?.lifecycle === "retired" ? t("此身份已退役。填写新名称可建立新身份，原记录保留。", "This identity is retired. Choose a new name to create a new identity; history is retained.") : t("名称用于管理员工，不会创建独立账号。已有登录与执行验证分别记录。", "Names organize employees without creating separate accounts. Sign-in and execution verification are recorded separately.");
        };
        newName.addEventListener("input", updateIdentity); updateIdentity();
        add.addEventListener("click", () => {
          const name = newName.value.trim(); if (!name) return;
          submitAction(add, box, () => api.addEmployee({ name, kind: employee.kind, connection_type: connectionType, entrypoint: employee.entrypoint || "", discovery_id: employee.discovery_id }), async () => {
            showToast(employee.execution_supported ? t(`${name} 已登记。请在项目中选择它作为成员。`, `${name} is registered. Choose it as a project member.`) : t("登记完成，自动执行暂不支持。", "Registered. Automatic execution is not supported yet."));
            await refresh(); await discover();
          });
        });
        list.append(el("div", { class: "discovery-row" }, avatar(employee.name), el("div", {}, el("h3", {}, employee.name),
          el("p", { class: "employee-kind" }, connectionType === "app" ? t("桌面应用", "Desktop app") : "CLI"), tag(employee.status), employee.detail ? el("p", {}, humanDetail(employee.detail)) : null,
          employee.entrypoint ? el("code", {}, employee.entrypoint) : null,
          el("p", { class: "field-hint" }, employee.execution_supported ? t("支持受管任务执行；完成任务后才标记执行已验证。", "Supports managed execution. Verified execution is recorded after a task completes.") : t("可登记和加入项目，自动执行暂不支持。", "Can be registered and added to projects. Automatic execution is unsupported.")),
          el("p", { class: "field-hint" }, t("原生登录", "Native sign-in"), " · ", tag(employee.auth_status || "unknown")), formField(t("员工名称", "Employee name"), newName), hint), add));
      });
    } catch (error) { if (list.isConnected) { list.replaceChildren(); formError(box, error); } }
    finally { retry.disabled = false; }
  }
  retry.addEventListener("click", discover); await discover();
}
function openTaskForm() {
  if (!project()) { openProjectForm(); return; }
  const people = assignableEmployees();
  if (!people.length) {
    if (projectEmployees().length) { selectView("members"); showToast(t("当前项目没有支持自动执行的在岗员工。请加入已支持的 CLI 员工，或恢复其接单状态。", "No active employees support automatic execution in this project. Add a supported CLI employee or resume assignments.")); }
    else openMemberForm();
    return;
  }
  const body = openForm(t("交代一件工作", "Assign a task"), t("写清目标、背景和验收标准。任务默认只读，修改文件需要你主动选择。", "Describe the goal, context, and acceptance criteria. Choose write access explicitly if the task needs it."));
  if (!body) return;
  const projectId = state.projectId;
  const title = el("input", { id: "task-title", required: true, maxlength: 240, placeholder: t("用一句话说明这件事", "Describe the work in one sentence") });
  const prompt = el("textarea", { id: "task-prompt", required: true, placeholder: t("要做什么？可参考哪些资料？什么结果算完成？", "What should be done? Which context matters? What counts as complete?") });
  const assignee = el("select", { id: "task-assignee", required: true }, people.map((employee) => el("option", { value: employee.id }, `${employee.name} · ${employee.kind}`)));
  const model = el("select", { id: "task-model" }, el("option", { value: "" }, t("复用员工设置", "Use employee settings")));
  const modelHint = el("p", { class: "field-hint", id: "task-model-hint" }, t("可选。保持默认即可；显式选择时，只显示员工当前服务确认的模型。", "Optional. Keep the default, or choose a model confirmed by this employee's service."));
  model.setAttribute("aria-describedby", modelHint.id);
  let modelRequest = 0;
  async function loadModels() {
    const requestId = ++modelRequest;
    const employee = people.find((item) => item.id === assignee.value);
    model.replaceChildren(el("option", { value: "" }, t("复用员工设置", "Use employee settings")));
    if (employee?.node_id && employee.node_id !== state.data.node?.id) {
      model.disabled = true;
      modelHint.textContent = t("远端模型列表尚未确认。本次任务复用这名员工在远端设备上的模型设置。", "Remote models have not been confirmed. This task uses the employee's model settings on its own device.");
      return;
    }
    if (employee?.kind !== "codex") {
      model.disabled = false;
      modelHint.textContent = t("这个员工使用它自己的模型设置。", "This employee uses its own model settings.");
      return;
    }
    model.disabled = true;
    modelHint.textContent = t("正在确认这个员工可用的模型…", "Checking this employee's available models…");
    try {
      const result = await api.models(employee.kind);
      if (requestId !== modelRequest || !model.isConnected) return;
      if (!Array.isArray(result.models)) throw new ApiError("invalid_response");
      for (const item of result.models) model.append(el("option", { value: item.id }, item.name || item.id));
      modelHint.textContent = result.models.length ? t("可选。这里只显示当前员工服务实际提供的模型；保持默认会复用员工设置。", "Optional. These models are advertised by the employee's current service. Keep the default to reuse employee settings.") : t("没有确认到可选模型，仍可复用员工设置；执行时会验证是否可用。", "No optional models were confirmed. You can still use employee settings; execution will verify availability.");
    } catch (error) {
      if (requestId !== modelRequest || !model.isConnected) return;
      modelHint.textContent = `${t("模型列表未确认，不会自动替换员工设置。", "The model list is unconfirmed. Employee settings will not be silently replaced.")} ${errorText(error)}`;
    } finally { if (requestId === modelRequest) model.disabled = false; }
  }
  const readOnly = el("input", { type: "radio", name: "permission", value: "read-only", checked: true });
  const write = el("input", { type: "radio", name: "permission", value: "workspace-write" });
  const permissions = el("fieldset", { class: "radio-group" }, el("legend", { class: "sr-only" }, t("任务权限", "Task permissions")),
    el("label", { class: "radio-choice" }, readOnly, el("div", {}, el("strong", {}, t("只读资料", "Read-only")), el("p", {}, t("阅读与分析项目，不修改文件。", "Read and analyze the project without editing files.")))),
    el("label", { class: "radio-choice" }, write, el("div", {}, el("strong", {}, t("可修改项目文件", "Project write access")), el("p", {}, t("允许在这个项目中完成编辑工作，额外权限仍需确认。", "Allow edits in this project. Additional permissions still need approval.")))));
  const box = errorBox();
  const form = el("form", {}, formField(t("任务名称", "Task title"), title), formField(t("交给谁", "Assign to"), assignee), el("div", { class: "form-field" }, el("label", { for: model.id }, t("模型（可选）", "Model (optional)")), model, modelHint), formField(t("工作说明", "Instructions"), prompt), el("div", { class: "form-field" }, el("span", { class: "field-hint" }, t("这次任务的权限", "Permissions for this task")), permissions), box);
  const end = footer(t("派发任务", "Assign task"));
  const runtimeBox = el("div", { id: "task-runtime-status", role: "status" });
  const isRemote = () => { const employee = people.find((item) => item.id === assignee.value); return Boolean(employee?.node_id && employee.node_id !== state.data.node?.id); };
  const canAssign = () => isRemote() || Boolean(state.data.runtime?.installed && state.data.runtime?.node_available);
  const updateRuntime = () => {
    if (!formDialog.open || !body.isConnected) return;
    end.submit.disabled = !canAssign() || Boolean(end.submit.dataset.loading);
    if (isRemote()) runtimeBox.replaceChildren(el("p", { class: "field-hint" }, t("这项工作在员工所在设备执行，不需要准备主控的执行环境。远端是否能执行将在任务状态中确认。", "Work runs on the employee's device, without setting up execution on the coordinator. The task status will confirm whether remote execution succeeds.")));
    else runtimeBox.replaceChildren(runtimeNotice({ force: true }) || el("p", { class: "field-hint" }, t("本机执行环境已准备好。", "Local execution environment is ready.")));
  };
  state.taskFormUpdate = updateRuntime;
  assignee.addEventListener("change", () => { loadModels(); updateRuntime(); });
  form.append(runtimeBox);
  form.append(end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!title.value.trim() || !prompt.value.trim() || !assignee.value || !canAssign()) return;
    submitAction(end.submit, box, () => api.createTask({ project_id: projectId, title: title.value.trim(), prompt: prompt.value.trim(), assignee_id: assignee.value, model: model.value || null, permission_mode: write.checked ? "workspace-write" : "read-only" }), async (result) => {
      formDialog.close();
      state.view = "tasks";
      state.taskFilter = "all";
      showToast(t("任务已排队，启动和执行进度会在任务里更新。", "Task queued. Startup and execution progress will appear in its record."));
      await refresh();
      const created = result.task || result;
      if (created.id) await openTaskDetail(created.id);
    });
  });
  body.append(form);
  updateRuntime();
  title.focus();
  loadModels();
}
function openResourceForm() {
  if (!project()) return openProjectForm();
  const body = openForm(t("添加共享资料", "Add shared context"), t("选择已有的 UTF-8 文本文件（不超过 1 MiB），例如需求说明或项目规则。", "Choose an existing UTF-8 text file up to 1 MiB, such as requirements or project rules."));
  if (!body) return;
  const projectId = state.projectId;
  const name = el("input", { id: "resource-name", required: true, maxlength: 160, placeholder: t("例如：需求说明", "For example: requirements") });
  const path = el("input", { id: "resource-path", required: true, placeholder: t("文本文件的绝对路径", "Absolute path to the text file") });
  const kind = el("select", { id: "resource-kind" }, [["document", t("文档", "Document")], ["rules", t("规则", "Rules")], ["reference", t("参考资料", "Reference")]].map(([value, label]) => el("option", { value }, label)));
  const box = errorBox();
  const form = el("form", {}, formField(t("名称", "Name"), name), formField(t("资料路径", "Resource path"), path), formField(t("类型", "Type"), kind), box);
  const end = footer(t("添加资料", "Add resource"));
  form.append(end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAction(end.submit, box, () => api.addResource({ project_id: projectId, name: name.value.trim(), kind: kind.value, path: path.value.trim() }), async () => {
      formDialog.close(); showToast(t("资料已加入项目。", "Resource added to the project.")); await refresh();
    });
  });
  body.append(form); name.focus();
}
function openMemoryForm() {
  if (!project()) return openProjectForm();
  const body = openForm(t("记录项目记忆", "Record project memory"), t("保存已经确认的决定、约束或经验。后续任务会获得这些项目记录。", "Save confirmed decisions, constraints, and lessons for future tasks."));
  if (!body) return;
  const projectId = state.projectId;
  const title = el("input", { id: "memory-title", required: true, maxlength: 200, placeholder: t("这条记录关于什么", "What is this record about?") });
  const content = el("textarea", { id: "memory-body", required: true, placeholder: t("写下已确认的背景、决定或约束。", "Write confirmed context, decisions, or constraints.") });
  const source = el("input", { id: "memory-source", maxlength: 240, placeholder: t("可选：会议、任务名称或文档", "Optional: meeting, task, or document") });
  const box = errorBox();
  const form = el("form", {}, formField(t("标题", "Title"), title), formField(t("内容", "Content"), content), formField(t("来源", "Source"), source), box);
  const end = footer(t("保存记忆", "Save memory"));
  form.append(end.node);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAction(end.submit, box, () => api.addMemory({ project_id: projectId, title: title.value.trim(), body: content.value.trim(), source: source.value.trim() }), async () => {
      formDialog.close(); showToast(t("项目记忆已保存。", "Project memory saved.")); await refresh();
    });
  });
  body.append(form); title.focus();
}
async function confirmDeleteMemory(memoryId) {
  const memory = state.data?.memories.find((item) => item.id === memoryId);
  if (!memory) return;
  const body = openForm(t("删除这条记忆？", "Delete this memory?"), t("后续任务将不再使用这条记录。已有任务记录会保留。", "Future tasks will no longer use this record. Existing task records remain."));
  if (!body) return;
  body.append(el("p", { class: "prose" }, memory.title));
  const box = errorBox();
  const cancel = button(t("保留", "Keep"), null);
  cancel.removeAttribute("data-action"); cancel.addEventListener("click", () => formDialog.close());
  const remove = button(t("删除记忆", "Delete memory"), null, { class: "danger" });
  remove.removeAttribute("data-action");
  remove.addEventListener("click", () => submitAction(remove, box, () => api.deleteMemory(memoryId), async () => { formDialog.close(); showToast(t("记忆已删除。", "Memory deleted.")); await refresh(); }));
  body.append(box, el("div", { class: "form-footer" }, cancel, remove)); cancel.focus();
}

async function openTaskDetail(taskId) {
  state.detailTaskId = taskId;
  state.detailFingerprint = "";
  state.detailError = "";
  detailContent.replaceChildren(el("div", { class: "detail-content" }, el("h2", { id: "detail-dialog-title" }, t("正在读取任务…", "Loading task…")), el("div", { class: "loading-rows" }, el("div", { class: "skeleton skeleton-row" }), el("div", { class: "skeleton skeleton-row" }))));
  if (!detailDialog.open) detailDialog.showModal();
  await loadTaskDetail();
}
async function loadTaskDetail({ silent = false } = {}) {
  if (!state.detailTaskId || !detailDialog.open) return;
  const taskId = state.detailTaskId;
  const requestId = ++state.detailRequest;
  try {
    const result = await api.taskDetail(taskId);
    if (requestId !== state.detailRequest || taskId !== state.detailTaskId || !detailDialog.open) return;
    if (!result.task || !Array.isArray(result.events) || !Array.isArray(result.permissions)) throw new ApiError("invalid_response");
    state.detailError = "";
    const fingerprint = JSON.stringify(result);
    if (silent && fingerprint === state.detailFingerprint) return;
    // Do not interrupt a human writing review notes with a polling refresh.
    if (silent && detailContent.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA") return;
    state.detailFingerprint = fingerprint;
    renderTaskDetail(result);
  } catch (error) {
    if (requestId !== state.detailRequest || taskId !== state.detailTaskId || !detailDialog.open) return;
    if (silent) {
      const message = errorText(error);
      if (message !== state.detailError) showToast(message, true);
      state.detailError = message;
      return;
    }
    const close = button(t("关闭", "Close"), null);
    close.removeAttribute("data-action"); close.addEventListener("click", () => detailDialog.close());
    const retry = button(t("重试", "Retry"), null, { class: "primary" });
    retry.removeAttribute("data-action"); retry.addEventListener("click", () => loadTaskDetail());
    detailContent.replaceChildren(el("div", { class: "detail-content" }, el("h2", { id: "detail-dialog-title" }, t("无法读取任务", "Could not load this task")), el("p", { class: "device-info" }, errorText(error)), el("div", { class: "approval-actions" }, retry, close)));
  }
}
function renderTaskDetail({ task, events, permissions }) {
  const close = el("button", { class: "icon-button", type: "button", "aria-label": t("关闭任务详情", "Close task details") }, icon("close"));
  close.addEventListener("click", () => detailDialog.close());
  const top = el("div", { class: "detail-top" }, el("div", { class: "dialog-heading" }, el("h2", { id: "detail-dialog-title" }, t("任务详情", "Task details")), close), el("h3", { class: "detail-title" }, task.title), el("div", { class: "detail-meta" }, tag(task.status), el("span", {}, employeeName(task.assignee_id)), el("span", {}, task.model || t("复用员工模型设置", "Employee model settings")), el("span", {}, formatDate(task.created_at, true))));
  const content = el("div", { class: "detail-content" });
  const box = errorBox();
  for (const permission of permissions.filter((item) => ["pending", "requested", "waiting", "waiting_approval", "expired"].includes(item.status))) {
    const call = permission.tool_call;
    const tool = typeof call === "string" ? call : call?.title || call?.name || t("员工请求执行工具操作。", "The employee requests a tool operation.");
    const operationDetails = typeof call === "object" && call !== null ? el("details", { class: "operation-details" }, el("summary", {}, t("查看完整操作内容", "View the full operation")), el("pre", {}, el("code", {}, JSON.stringify(call, null, 2)))) : null;
    const expiryDate = permission.expires_at === undefined || permission.expires_at === null ? null : new Date(typeof permission.expires_at === "number" ? permission.expires_at * 1000 : permission.expires_at);
    const expires = expiryDate && !Number.isNaN(expiryDate.getTime()) ? expiryDate : null;
    const hasExpired = () => permission.status === "expired" || (expires && expires.getTime() <= Date.now());
    const expired = hasExpired();
    const remaining = expires ? Math.max(0, Math.ceil((expires.getTime() - Date.now()) / 60000)) : null;
    const deadline = expires ? el("p", { class: `permission-deadline${expired ? " expired" : ""}` },
      t("授权截止", "Permission deadline"), " · ", el("time", { datetime: expires.toISOString() }, new Intl.DateTimeFormat(english ? "en" : "zh-CN", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" }).format(expires)),
      !expired ? el("span", {}, t(`读取时剩余约 ${remaining} 分钟，以截止时间为准。`, `About ${remaining} minutes remained when loaded. The deadline is authoritative.`)) : null) : null;
    const allow = button(t("仅允许这一次", "Allow once"), null, { class: "primary small", disabled: expired });
    const deny = button(t("拒绝", "Deny"), null, { class: "small" });
    allow.removeAttribute("data-action"); deny.removeAttribute("data-action");
    const decide = (control, decision) => submitAction(control, box, () => api.decidePermission(task.id, permission.request_id, { decision }), async () => {
      showToast(decision === "allow_once" ? t("已授权这一次操作。", "This operation was allowed once.") : t("已拒绝这次操作。", "This operation was denied."));
      await loadTaskDetail(); await refresh({ silent: true });
    });
    allow.addEventListener("click", () => {
      if (hasExpired()) { allow.disabled = true; formError(box, new ApiError(t("授权请求已过期，不能继续允许。请让员工重新提出请求。", "This permission request expired and cannot be allowed. The employee must request permission again."))); return; }
      decide(allow, "allow_once");
    });
    deny.addEventListener("click", () => decide(deny, "deny"));
    content.append(el("section", { class: "approval-panel" }, el("h3", {}, expired ? t("授权请求已过期", "Permission request expired") : t("员工需要你确认这次操作", "The employee needs permission for this operation")), el("p", { class: "prose" }, tool || t("操作细节未记录，请拒绝并检查任务记录。", "The operation details are missing. Deny it and check the task record.")), deadline, operationDetails,
      expired ? el("p", { class: "permission-expired" }, t("这次请求已失效，不能继续授权。需要员工重新提出请求。", "This request is no longer valid. The employee must request permission again.")) : null,
      !expired ? el("div", { class: "approval-actions" }, allow, deny) : null));
  }
  content.append(el("section", { class: "detail-section" }, el("h3", {}, t("工作说明", "Instructions")), el("p", { class: "prose" }, task.prompt || t("没有工作说明。", "No instructions recorded."))));
  if (task.result) content.append(el("section", { class: "detail-section" }, el("h3", {}, t("员工交付", "Employee output")), el("div", { class: "result-block" }, el("p", { class: "prose" }, typeof task.result === "string" ? task.result : JSON.stringify(task.result, null, 2)))));
  if (task.error) content.append(el("section", { class: "detail-section" }, el("h3", {}, t("需要处理的问题", "Issue to resolve")), el("p", { class: "prose" }, typeof task.error === "string" ? task.error : task.error.message || JSON.stringify(task.error))));
  if (task.error?.code === "MODEL_UNSUPPORTED") content.append(el("p", { class: "field-hint" }, t("这个模型不适用于员工当前的登录方式。新建任务时，从模型列表选择服务实际提供的模型；原任务保留为失败，不会自动换模型重跑。", "This model is not supported by the employee's current sign-in method. Create a new task and select a model advertised by the service. The failed task is retained and is not automatically retried with another model.")));
  if (task.status === "review") {
    const note = el("textarea", { id: "review-note", placeholder: t("可选：写下验收意见；退回时说明还需要完成什么。", "Optional review note. When returning work, explain what still needs to be done."), "aria-label": t("验收意见", "Review note") });
    const accept = button(t("通过验收", "Accept work"), null, { class: "primary", icon: "check" });
    const reject = button(t("退回补充", "Return for follow-up"), null);
    accept.removeAttribute("data-action"); reject.removeAttribute("data-action");
    const review = (control, decision) => submitAction(control, box, () => api.reviewTask(task.id, { decision, note: note.value.trim() }), async () => {
      showToast(decision === "accept" ? t("任务已通过验收。", "Task accepted.") : t("任务已退回，后续状态以任务记录为准。", "Task returned. Check the task record for its next status."));
      await loadTaskDetail(); await refresh({ silent: true });
    });
    accept.addEventListener("click", () => review(accept, "accept")); reject.addEventListener("click", () => review(reject, "reject"));
    content.append(el("section", { class: "review-panel" }, el("h3", {}, t("成果已交付，等你验收", "The work is ready for your review")), el("p", {}, t("通过后才会记为已完成。需要补充时可以退回并写明要求。", "The task is complete only after you accept it. Return it with clear follow-up instructions if needed.")), note, el("div", { class: "approval-actions" }, accept, reject)));
  }
  const timelineEvents = events.filter((event) => event.payload?.event?.type !== "text_delta");
  content.append(box, el("section", { class: "detail-section" }, el("h3", {}, t("工作时间线", "Work timeline")), timelineEvents.length ? el("ol", { class: "timeline" }, timelineEvents.map((event) => {
    const activity = event.payload?.event;
    const toolTitle = activity?.toolCall?.title || activity?.tool_call?.title || activity?.title;
    return el("li", { class: "timeline-item" }, el("span", { class: "timeline-dot", "aria-hidden": "true" }), el("div", {}, el("p", { class: "timeline-message" }, typeof toolTitle === "string" && toolTitle ? toolTitle : event.message || t("任务状态已更新。", "Task state updated.")), el("time", { class: "timeline-time", datetime: event.created_at || "" }, formatDate(event.created_at, true))));
  })) : el("p", { class: "inline-empty" }, t("还没有记录到工作事件。", "No work events have been recorded yet."))));
  const detailRefresh = button(t("刷新记录", "Refresh record"), null, { class: "ghost small", icon: "refresh" });
  detailRefresh.removeAttribute("data-action"); detailRefresh.addEventListener("click", () => loadTaskDetail());
  const footerRow = el("div", { class: "detail-footer" }, detailRefresh);
  if (liveStatuses.has(task.status)) {
    const cancel = button(t("取消任务", "Cancel task"), null, { class: "danger small" });
    cancel.removeAttribute("data-action");
    cancel.addEventListener("click", () => submitAction(cancel, box, () => api.cancelTask(task.id), async () => { showToast(t("取消请求已提交。", "Cancellation requested.")); await loadTaskDetail(); await refresh({ silent: true }); }));
    footerRow.append(cancel);
  }
  content.append(footerRow);
  detailContent.replaceChildren(top, content);
}

async function installRuntime(control) {
  if (state.runtimeInstalling) return;
  state.runtimeInstalling = true;
  if (control) control.disabled = true;
  try {
    await api.installRuntime();
    showToast(t("正在准备执行环境，完成后状态会自动更新。", "Preparing the execution environment. Its state will update automatically."));
    if (formDialog.open && !state.taskFormUpdate) formDialog.close();
    await refresh();
  } catch (error) { state.runtimeInstalling = false; showToast(errorText(error), true); render(); }
}
const actions = {
  "new-project": openProjectForm, discover: openEmployeeForm, "add-member": openMemberForm,
  "new-task": openTaskForm, "add-resource": openResourceForm,
  "add-memory": openMemoryForm, refresh: () => refresh(),
  "install-runtime": installRuntime,
  "fleet-start": openFleetStartForm, "fleet-invite": openFleetInviteForm,
  "fleet-join": openFleetJoinForm, "fleet-stop": () => confirmFleetAction("stop"),
  "fleet-leave": () => confirmFleetAction("leave"),
  "quit-application": openQuitForm,
};
document.addEventListener("click", (event) => {
  const target = event.target.closest("button, a");
  if (!target) return;
  if (state.applicationStopped) { event.preventDefault(); return; }
  if (target.matches(".brand")) { event.preventDefault(); selectView("employees"); }
  else if (target.dataset.project) selectProject(target.dataset.project);
  else if (target.dataset.view) selectView(target.dataset.view);
  else if (target.dataset.task) openTaskDetail(target.dataset.task);
  else if (target.dataset.filter) { state.taskFilter = target.dataset.filter; render(); }
  else if (target.dataset.deleteMemory) confirmDeleteMemory(target.dataset.deleteMemory);
  else if (actions[target.dataset.action]) actions[target.dataset.action](target);
});
document.getElementById("refresh-button").addEventListener("click", () => refresh());
document.getElementById("theme-button").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(theme);
  try { localStorage.setItem("agent-mailbox.workbench.theme", theme); } catch { /* optional */ }
});
document.getElementById("sidebar-toggle").addEventListener("click", () => setSidebar(!document.getElementById("sidebar").classList.contains("open")));
document.getElementById("sidebar-backdrop").addEventListener("click", () => setSidebar(false));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") setSidebar(false); });
detailDialog.addEventListener("close", () => { state.detailTaskId = null; ++state.detailRequest; state.detailFingerprint = ""; });
for (const dialog of [formDialog, detailDialog]) {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
}
for (const control of document.querySelectorAll("[data-view]")) {
  const span = control.querySelector("span");
  if (span) span.textContent = navLabels[control.dataset.view];
}
document.querySelector(".brand small").textContent = t("团队工作台", "Team workbench");
document.querySelector(".sidebar-heading > span").textContent = t("项目", "Projects");
document.querySelector("#quit-application-button > span").textContent = t("退出应用", "Quit application");
document.querySelector(".skip-link").textContent = t("跳到工作区", "Skip to workspace");
document.querySelector('[data-action="new-project"]').setAttribute("aria-label", t("新建项目", "New project"));
document.getElementById("refresh-button").setAttribute("aria-label", t("刷新工作台", "Refresh workbench"));
let theme = "light";
try { theme = localStorage.getItem("agent-mailbox.workbench.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); } catch { /* use light */ }
applyTheme(theme);
refresh();
async function applyChanges() {
  if (state.applicationStopping || state.applicationStopped) return;
  if (document.visibilityState !== "visible" || (formDialog.open && !state.taskFormUpdate)) { state.changesPending = true; return; }
  if (state.refreshing) { queueChanges(); return; }
  state.changesPending = false;
  state.streamError = false;
  await refresh({ silent: true });
  if (state.detailTaskId && detailDialog.open) await loadTaskDetail({ silent: true });
}
function queueChanges() {
  if (state.applicationStopping || state.applicationStopped) return;
  state.changesPending = true;
  window.clearTimeout(state.changeTimer);
  state.changeTimer = window.setTimeout(applyChanges, 250);
}
function connectChanges() { return api.subscribeChanges(queueChanges, (error) => {
  if (state.applicationStopping || state.applicationStopped) return;
  if (!state.streamError && state.data) showToast(t("实时更新暂时中断，正在重新连接；也可点击刷新。", "Live updates were interrupted. Reconnecting; you can also refresh manually."), true);
  state.streamError = true;
  if (state.data) setNotice(`${t("实时更新正在重新连接。", "Live updates are reconnecting.")} ${errorText(error)}`, true);
}, () => {
  if (state.streamError && !state.error) setNotice(null);
  state.streamError = false;
}); }
let stopChanges = connectChanges();
formDialog.addEventListener("close", () => {
  state.taskFormUpdate = null;
  if (!formDialog.open) {
    const invitation = formContent.querySelector("#fleet-invitation, #fleet-join-invitation");
    if (invitation) invitation.value = "";
  }
  queueChanges();
});
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") queueChanges(); });
window.addEventListener("pagehide", () => stopChanges());
window.addEventListener("pageshow", (event) => { if (event.persisted && !state.applicationStopped) { stopChanges = connectChanges(); queueChanges(); } });
