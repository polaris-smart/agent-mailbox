import { api, ApiError } from "./api.js";

let languagePreference = "auto";
try { languagePreference = localStorage.getItem("agent-mailbox.workbench.language") || "auto"; } catch { /* optional */ }
if (!["auto", "zh-CN", "en"].includes(languagePreference)) languagePreference = "auto";
const english = languagePreference === "en" || (languagePreference === "auto" && !navigator.language.toLowerCase().startsWith("zh"));
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
  legalDocuments: {},
  updates: null, updatesLoading: false, updatesError: null, updatesAction: "", updatesAttempted: false, updatePauseUnconfirmed: false,
  taskFormUpdate: null,
  employeeQuery: "", employeeFilter: "all", mailboxEmployeeId: "", mailboxFolder: "inbox",
  messageDrafts: {}, messages: {}, messagesLoading: false, messagesError: null, dragEmployeeId: null,
};
try { state.projectId = sessionStorage.getItem("agent-mailbox.workbench.project") || ""; } catch { /* optional */ }

try { if (sessionStorage.getItem("agent-mailbox.workbench.language-view") === "about") { state.view = "about"; sessionStorage.removeItem("agent-mailbox.workbench.language-view"); } } catch { /* optional */ }

const navLabels = {
  overview: t("项目概览", "Overview"), tasks: t("任务", "Tasks"), activity: t("工作日志", "Work log"),
  employees: t("所有员工", "All employees"), members: t("项目成员", "Project members"), messages: t("项目消息", "Messages"), resources: t("资料与记忆", "Resources & memory"),
  mailboxes: t("员工信箱", "Employee mailboxes"), devices: t("设备", "Devices"), about: t("关于与设置", "About & settings"),
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
    KNOWLEDGE_INDEX_BUSY: t("索引工具正在写入，请等它完成后再检查。", "The index tool is writing. Wait for it to finish, then check again."),
    authorization_required: t("请从本机工作台入口重新打开页面以连接服务。", "Reopen this page from the workbench launcher to connect."),
    request_timeout: t("服务响应超时，请稍后重试。", "The service took too long to respond. Try again."),
    connection_failed: t("无法连接工作台服务，请确认它正在运行后重试。", "Could not connect to the workbench. Check that it is running and try again."),
    invalid_response: t("服务返回了无法读取的数据，请刷新后重试。", "The service returned an unreadable response. Refresh and try again."),
    request_failed: t("操作没有完成，请重试。", "The operation did not complete. Try again."),
  };
  return messages[error.code] || messages[error.message] || error.message || t("操作没有完成，请重试。", "The operation did not complete. Try again.");
}
function humanDetail(value) {
  const messages = {
    "The managed execution adapter requires authentication; native sign-in alone is not execution verification.": t("后台执行认证未通过；原生登录正常也不代表执行已接通。请查看接入指引。", "Managed execution authentication failed. Native sign-in alone does not verify execution; see Connection guide."),
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
  const modal = open && matchMedia("(max-width: 760px)").matches;
  document.querySelector(".main-shell").inert = modal;
  document.body.classList.toggle("drawer-open", modal);
  document.getElementById("sidebar-backdrop").hidden = !open;
  document.getElementById("sidebar-toggle").setAttribute("aria-expanded", String(open));
  if (open) sidebar.querySelector(".brand").focus();
  else if (returnFocus) document.getElementById("sidebar-toggle").focus();
}
function selectProject(id) {
  if (!state.data?.projects.some((item) => item.id === id)) return;
  document.getElementById("project-switcher").open = false;
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
  if (view === "about") loadUpdates();
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
    const active = control.classList.contains("nav-item") && control.dataset.view === state.view;
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
  document.getElementById("current-project-name").textContent = current?.name || t("选择项目", "Select project");
  document.getElementById("current-project-symbol").textContent = (current?.name || "P").slice(0, 1);
  document.getElementById("project-switch-hint").textContent = t("切换项目", "Switch project");
  for (const control of document.querySelectorAll(".project-nav [data-view]")) control.disabled = !current;
  document.getElementById("breadcrumb-project").textContent = ["employees", "mailboxes", "about", "devices"].includes(state.view) ? t("工作台", "Workbench") : current?.name || t("工作台", "Workbench");
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
  const result = [heading(navLabels.tasks, t("分配任务 → 员工通过邮件交接 → Human 验收成果。全过程保留记录。", "Assign work → employees hand off through mail → Human accepts the outcome. Each step is recorded."), [button(t("新任务", "New task"), "new-task", { class: "primary", icon: "plus" })]), runtimeNotice(), filterRow, list];
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

function executionGuide(employee, error = null) {
  const code = error?.code;
  let title, explanation, next;
  if (!employee?.execution_supported) {
    title = t("已登记，自动执行尚未接入", "Registered; execution not connected");
    explanation = t("发现 app 或 CLI 只证明它已安装。这个入口可以加入项目，目前没有自动触发适配器。", "Discovery confirms installation. This entry can join projects, but has no automatic execution adapter yet.");
    next = t("可先整理项目资料；执行任务请选择已有适配器的 CLI 员工。", "Organize project resources now; choose a CLI employee with an execution adapter for tasks.");
  } else if (code === "AUTH_REQUIRED" || employee.status === "auth_required" || employee.auth_status === "auth_required") {
    title = t("执行认证未通过", "Execution authentication failed");
    explanation = employee.auth_status === "authenticated" ? t("原生登录检查正常，但后台执行进程没有取得可用授权。这可能是执行适配或凭据读取问题，不能据此判断你没登录。", "Native sign-in is confirmed, but the managed execution process could not authenticate. This may be an adapter or credential-access issue; it does not prove you are signed out.") : t("后台执行没有可用授权。先检查原生工具是否能正常使用；登录状态未知时，不会猜测凭据是否有效。", "Managed execution could not authenticate. Check whether the native tool works; an unknown sign-in state does not prove credentials are valid.");
    next = employee.auth_status === "authenticated" ? t("先在原生工具中完成一个简单请求，再点“检查连接”。若原生能执行、这里仍失败，请保留错误和入口路径用于排查接入；无需复制 key。", "Try a simple request in the native tool, then Check connection. If native execution works but this still fails, keep the error and entry path for adapter diagnosis. Do not copy keys.") : t("在终端完成原生登录，再点“检查连接”。这只复查登录状态；首次任务成功后才算执行已验证。", "Sign in using the native terminal command, then Check connection. This only rechecks sign-in; a successful task verifies execution.");
  } else if (code === "MODEL_UNSUPPORTED") {
    title = t("当前模型不可用", "Selected model unavailable");
    explanation = t("执行接口拒绝了任务所选模型；这与未登录是两种问题。", "The execution service rejected the selected model; this is different from a sign-in failure.");
    next = t("新建任务时选择模型列表中服务提供的模型。原任务保留，不会自动换模型或重跑。", "Create a new task with a model advertised by the service. The original task stays recorded; no automatic model switch or retry.");
  } else if (["RUNTIME_MISSING", "AGENT_UNAVAILABLE"].includes(code)) {
    title = t("执行组件尚未就绪", "Execution components unavailable");
    explanation = t("员工安装与执行环境是两个独立检查。", "Employee installation and execution runtime are separate checks.");
    next = t("先使用“准备执行环境”，完成后重新检查连接，再创建任务。", "Use Set up execution, then check the connection and create a task.");
  } else if (code) {
    title = t("这次执行没有完成", "This execution did not complete");
    explanation = t("请结合任务记录中的原始错误判断原因，不把超时、权限拒绝或断线都当成登录失败。", "Use the recorded error to identify the cause. Timeouts, permission denials and disconnections are not all sign-in failures.");
    next = t("权限问题查看授权记录；超时或断线先确认工具与服务可用，再决定是否创建新任务。不会自动重跑。", "For permission issues, inspect the approval record. For timeouts or disconnections, check tool and service availability before assigning new work. No automatic retry.");
  } else {
    title = employee.execution_verified ? t("已有成功执行记录", "Successful execution recorded") : t("等待首次执行验证", "First execution not verified");
    explanation = t("检查连接只确认入口和原生登录，不会调用模型或重跑任务。执行验证表示历史成功记录，不保证下一次请求成功。", "Check connection checks installation and native sign-in only; it does not call a model or rerun tasks. Verification records past success, not a guarantee for the next request.");
    next = t("加入项目后，派一个小任务；完成并返回结果后再人工验收。", "Join a project and assign a small task, then review the returned result.");
  }
  return el("section", { class: "connection-guide" }, el("h3", {}, title), el("p", {}, explanation), el("p", {}, next), code ? el("p", { class: "field-hint" }, t("诊断代码", "Diagnostic code"), " · ", el("code", {}, code)) : null);
}
async function openConnectionGuide(employee) {
  const body = openForm(t(`${employee.name} · 接入验证`, `${employee.name} · Connection verification`), t("本次检查：安装、登录与实际执行分开；查看此页不会调用模型。", "Current check: installation, sign-in, and execution are separate. Viewing this page does not run a model."));
  if (!body) return;
  const projectId = state.projectId;
  const stage = el("div", { class: "onboarding-checks", role: "status" });
  const box = errorBox();
  body.append(stage, box);
  body.append(el("p", { class: "field-hint" }, t("员工入口", "Employee entry point"), " · ", el("code", {}, employee.entrypoint || t("未绑定入口", "No bound entry point"))));
  if (["claude", "codex"].includes(employee.kind) && employee.auth_status !== "authenticated" && (!employee.node_id || employee.node_id === state.data.node?.id)) body.append(el("p", { class: "field-hint" }, t("原生登录可在本机终端完成：", "Sign in through the native tool in a local terminal: "), el("code", {}, employee.kind === "claude" ? "claude auth login" : "codex login")));
  let requestId = 0;
  async function load() {
    const current = ++requestId;
    stage.replaceChildren(el("p", {}, t("正在读取接入检查…", "Loading connection checks…")));
    try {
      const result = await api.employeeOnboarding(employee.id, projectId);
      if (current !== requestId || !body.isConnected || !formDialog.open) return;
      if (!Array.isArray(result.checks)) throw new ApiError("invalid_response");
      stage.replaceChildren(el("ul", { class: "connection-checklist" }, result.checks.map(item => el("li", { class: `connection-check ${item.status}` },
        icon(item.status === "passed" ? "check" : item.status === "blocked" ? "warning" : "clock"),
        el("div", {}, el("strong", {}, item.message?.[english ? "en" : "zh"] || item.id), el("span", { class: "check-status" }, item.status === "passed" ? t("已通过", "Passed") : item.status === "blocked" ? t("需处理", "Needs attention") : t("未确认", "Unconfirmed")), el("p", {}, item.action?.[english ? "en" : "zh"] || ""))))));
      if (result.shared_native_identity) stage.append(el("p", { class: "field-hint" }, t("同类员工默认共享本机原生工具登录；员工身份不等于独立模型账号。", "Employees of the same tool share this device's native sign-in by default. Employee identity is not a separate model account.")));
      if (result.probe) {
        const probe = result.probe;
        const open = button(probe.verified ? t("查看验证记录", "View verified run") : t("查看验证进度", "View verification progress"), null);
        open.addEventListener("click", () => { formDialog.close(); openTaskDetail(probe.task_id); });
        stage.append(el("p", {}, probe.verified ? t("工具调用与任务结束已确认。", "Tool calls and task completion are confirmed.") : t("验证尚未完成，请查看实际记录。", "Verification is not complete. Check its actual record.")), open);
      }
      const model = el("select", { id: "probe-model" }, el("option", { value: "" }, t("保留原生模型设置", "Keep native model settings")));
      const modelHint = el("p", { class: "field-hint", id: "probe-model-hint" }, t("可选。默认保持原生设置，不会自动替换模型；这里只查询模型元数据，不调用模型生成内容。", "Optional. Native settings stay unchanged by default. This only reads model metadata, without generating content."));
      model.setAttribute("aria-describedby", modelHint.id);
      stage.append(formField(t("验证模型（可选）", "Verification model (optional)"), model), modelHint);
      if (employee.kind === "codex" && result.can_verify) {
        model.disabled = true;
        api.models(employee.kind).then(value => {
          if (current !== requestId || !model.isConnected || !formDialog.open) return;
          if (!Array.isArray(value.models)) throw new ApiError("invalid_response");
          for (const item of value.models) model.append(el("option", { value: item.id }, item.name || item.id));
          modelHint.textContent = value.models.length ? t("保持原生设置，或选择执行服务确认支持的模型。默认模型不受支持时，请显式选择后再验证；不会修改原生工具配置。", "Keep native settings or explicitly select a model advertised by the execution service. If the default is unsupported, select one before testing. Native configuration is not changed.") : t("未确认可选模型。仍保留原生设置，实际验证可能返回模型不受支持。", "No optional models were confirmed. Native settings remain unchanged; the test may report an unsupported model.");
        }).catch(error => {
          if (current !== requestId || !model.isConnected || !formDialog.open) return;
          modelHint.textContent = `${t("模型列表未确认，仍保留原生设置。", "The model list is unconfirmed. Native settings remain unchanged.")} ${errorText(error)}`;
        }).finally(() => { if (current === requestId && model.isConnected) model.disabled = false; });
      } else if (employee.kind !== "codex" || !result.can_verify) model.disabled = true;
      const verify = button(t("运行只读接入测试", "Run read-only connection test"), null, { class: "primary", disabled: result.can_verify !== true });
      stage.append(el("p", { class: "field-hint" }, t("点击会调用员工的原生模型、消耗额度，并可能需要你确认工具权限；会读取项目上下文并写一条验证笔记，不修改项目文件。", "Clicking uses the employee's native model quota and may require tool approval. It reads project context and writes a verification note, without editing project files.")), verify);
      verify.addEventListener("click", () => submitAction(verify, box, () => api.verifyEmployee(employee.id, projectId, model.value), async result => {
        if (!body.isConnected || !formDialog.open) return;
        formDialog.close(); await refresh();
        const taskId = result.task?.id || result.task_id;
        if (taskId) await openTaskDetail(taskId);
      }));
    } catch (error) {
      if (current !== requestId || !body.isConnected || !formDialog.open) return;
      stage.replaceChildren(executionGuide(employee)); formError(box, error);
    }
  }
  const retry = button(t("重新读取检查", "Refresh checks"), null);
  retry.addEventListener("click", load); body.append(retry);
  await load();
}

function employeeRow(employee, { membership = false } = {}) {
  const lifecycle = employee.lifecycle || "active";
  const controls = el("div", { class: "employee-actions" });
  const check = button(t("检查连接", "Check connection"), null, { class: "small", icon: "refresh", attrs: { "aria-label": t(`检查 ${employee.name} 的连接`, `Check connection for ${employee.name}`) } });
  check.addEventListener("click", () => openConnectionGuide(employee));
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
    el("div", {}, el("dt", {}, t("原生登录 · 上次记录", "Native sign-in · Last recorded")), el("dd", {}, tag(employee.auth_status || "unknown"))),
    el("div", {}, el("dt", {}, t("执行验证", "Execution check")), el("dd", {}, verified ? t("已验证", "Verified") : t("尚未验证", "Not verified"))));
  const row = el("article", { class: "employee-row employee-registry-row", "data-employee-id": employee.id, draggable: String(!membership && lifecycle !== "retired") }, avatar(employee.name),
    el("div", { class: "employee-profile" }, el("div", { class: "employee-title" }, el("h2", { class: "employee-name" }, employee.name), tag(lifecycle)),
      el("div", { class: "employee-kind" }, `${employee.kind} · ${connection}`), employee.entrypoint ? el("p", { class: "employee-detail" }, el("code", {}, employee.entrypoint)) : null,
      employee.detail ? el("p", { class: "employee-detail" }, humanDetail(employee.detail)) : null,
      employee.lifecycle_reason ? el("p", { class: "employee-reason" }, t("调整原因", "Reason"), " · ", employee.lifecycle_reason) : null,
      facts), controls);
  row.classList.add("employee-card");
  const projects = state.data.projects.filter(item => employee.project_ids?.includes(item.id) || employee.project_id === item.id);
  const mailbox = button(t("查看信箱", "Open mailbox"), null, { class: "small employee-mailbox-button", icon: "document" });
  mailbox.addEventListener("click", () => { state.mailboxEmployeeId = employee.id; state.mailboxFolder = "inbox"; selectView("mailboxes"); });
  const join = button(t("选择项目", "Choose project"), null, { class: "small", icon: "plus", disabled: lifecycle === "retired" });
  join.addEventListener("click", () => openEmployeeProjects(employee));
  const details = el("details", { class: "employee-configuration" }, el("summary", {}, t("连接与管理", "Connection & management")),
    facts, employee.entrypoint ? el("p", { class: "employee-detail" }, el("code", {}, employee.entrypoint)) : null,
    employee.detail ? el("p", { class: "employee-detail" }, humanDetail(employee.detail)) : null,
    employee.lifecycle_reason ? el("p", { class: "employee-reason" }, employee.lifecycle_reason) : null, controls);
  const executionLabel = verified ? t("执行已验证", "Execution verified") : employee.execution_supported ? t("支持任务 · 尚未验证", "Tasks supported · Unverified") : t("自动执行待接入", "Execution not connected");
  row.replaceChildren(el("header", { class: "employee-card-header" }, avatar(employee.kind === "codex" ? "Cx" : employee.kind === "claude" ? "CC" : employee.kind.slice(0,2).toUpperCase()),
    el("div", { class: "employee-card-identity" }, el("h2", { class: "employee-name" }, employee.name), el("p", { class: "employee-kind" }, employee.kind, " · ", deviceName(employee.node_id))),
    el("span", { class: "employee-entry-type" }, connection)),
    el("div", { class: "employee-card-status" }, el("span", { class: `execution-pill ${verified ? "verified" : ""}` }, executionLabel), lifecycle !== "active" ? tag(lifecycle) : null),
    el("p", { class: "employee-sign-in" }, t("原生登录 · 上次记录", "Native sign-in · Last recorded"), " · ", statusLabels[employee.auth_status] || statusLabels.unknown),
    el("div", { class: "employee-projects" }, projects.length ? projects.map(item => el("span", { class: "employee-project-chip", title: item.name }, icon("folder"), item.name)) : el("span", { class: "muted" }, t("尚未加入项目", "No project yet"))),
    el("div", { class: "employee-card-actions" }, mailbox, !membership ? join : null), details);
  if (!membership && lifecycle !== "retired") {
    row.addEventListener("dragstart", (event) => {
      if (event.target.closest("button, a, input, select, summary, details")) { event.preventDefault(); return; }
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
function renderEmployeeGrid() {
  const query = state.employeeQuery.trim().toLocaleLowerCase();
  const people = state.data.employees.filter(employee => {
    const filter = state.employeeFilter;
    if (filter === "cli" && employee.connection_type !== "cli") return false;
    if (filter === "app" && employee.connection_type !== "app") return false;
    if (filter === "supported" && !employee.execution_supported) return false;
    return !query || [employee.name, employee.kind, deviceName(employee.node_id), ...state.data.projects.filter(p => employee.project_ids?.includes(p.id)).map(p => p.name)].some(value => String(value).toLocaleLowerCase().includes(query));
  });
  const target = document.getElementById("employee-grid");
  if (target) target.replaceChildren(...(people.length ? people.map(employee => employeeRow(employee)) : [empty(t("没有匹配的员工", "No matching employees"), t("试试其他名称、项目或入口类型。", "Try another name, project or entry type."))]));
  const count = document.getElementById("employee-result-count"); if (count) count.textContent = t(`${people.length} 个入口`, `${people.length} entries`);
}
function renderEmployees() {
  const people = state.data.employees;
  const search = el("input", { id: "employee-search", type: "search", value: state.employeeQuery, placeholder: t("搜索员工、工具或项目…", "Search employees, tools or projects…"), "aria-label": t("搜索员工", "Search employees") });
  search.addEventListener("input", () => { state.employeeQuery = search.value; renderEmployeeGrid(); });
  const filters = el("div", { class: "employee-filters", "aria-label": t("员工入口类型", "Employee entry type") });
  for (const [value, label] of [["all", t("全部", "All")], ["cli", "CLI"], ["app", t("桌面应用", "Apps")], ["supported", t("支持任务", "Task support")]]) {
    const control = button(label, null, { class: "small", attrs: { "aria-pressed": String(state.employeeFilter === value) } });
    control.addEventListener("click", () => { state.employeeFilter = value; filters.querySelectorAll("button").forEach(item => item.setAttribute("aria-pressed", String(item === control))); renderEmployeeGrid(); }); filters.append(control);
  }
  const grid = el("div", { id: "employee-grid", class: "employee-grid" }, people.filter(employee => !state.employeeQuery || employee.name.toLocaleLowerCase().includes(state.employeeQuery.toLocaleLowerCase())).map(employee => employeeRow(employee)));
  queueMicrotask(renderEmployeeGrid);
  return [heading(navLabels.employees, t("你的 AI 员工，在这里相识，在项目里协作。", "Meet your AI employees here. Collaborate in projects."), [button(t("发现员工", "Discover employees"), "discover", { class: "primary", icon: "plus" }), button(t("创建项目", "Create project"), "new-project", { icon: "folder" })]),
    el("div", { class: "employee-directory-toolbar" }, el("label", { class: "employee-search" }, icon("search"), search), filters, el("span", { id: "employee-result-count", class: "muted small-text" }, t(`${people.length} 个入口`, `${people.length} entries`))),
    people.length ? grid : empty(t("从你已有的员工开始", "Start with your existing employees"), t("发现并登记这台设备上的 AI 工具，无需先创建项目。", "Discover tools on this device before creating a project."), "discover", t("发现已有员工", "Discover existing employees"), "users"),
    el("p", { class: "directory-note" }, t("入口不等于独立账号；登录、执行支持和执行验证分别记录。可拖入左侧项目，或点“选择项目”。", "An entry is not a separate account. Sign-in, task support and verified execution are distinct. Drag into a project or Choose project."))];
}
function openEmployeeProjects(employee) {
  if (!state.data.projects.length) { openProjectForm(); return; }
  const body = openForm(t(`${employee.name} · 选择项目`, `${employee.name} · Choose project`), t("加入项目不会启动执行。一个员工可加入多个项目。", "Joining does not start execution. An employee may join several projects.")); if (!body) return;
  const error = errorBox(); body.append(error);
  for (const item of state.data.projects) {
    const joined = employee.project_ids?.includes(item.id) || employee.project_id === item.id;
    const add = button(joined ? t("已加入", "Joined") : t("加入项目", "Join project"), null, { class: "small", disabled: joined });
    add.addEventListener("click", () => submitAction(add, error, () => api.addProjectMember(item.id, employee.id), async () => { await refresh(); add.textContent = t("已加入", "Joined"); add.disabled = true; showToast(t(`${employee.name} 已加入项目。`, `${employee.name} joined the project.`)); }));
    body.append(el("div", { class: "member-picker-row" }, icon("folder"), el("strong", {}, item.name), add));
  }
}
function renderMailboxes() {
  const employees = state.data.employees;
  const employee = employees.find(item => item.id === state.mailboxEmployeeId) || employees[0];
  if (!employee) return [heading(navLabels.mailboxes), empty(t("先登记一位员工", "Register an employee first"), t("登记后，这里可以查看它在项目中的收信与发信。", "Once registered, view its project correspondence here."), "discover", t("发现员工", "Discover employees"))];
  state.mailboxEmployeeId = employee.id;
  const selector = el("select", { id: "mailbox-employee", "aria-label": t("选择员工信箱", "Choose employee mailbox") }, employees.map(item => el("option", { value: item.id }, item.name))); selector.value = employee.id;
  selector.addEventListener("change", () => { state.mailboxEmployeeId = selector.value; render(); });
  const memberships = new Set(employee.project_ids || []); if (employee.project_id) memberships.add(employee.project_id);
  const relevant = (state.data.messages || []).filter(message => state.mailboxFolder === "sent" ? message.sender_id === employee.id : state.mailboxFolder === "group" ? !message.recipient_id && memberships.has(message.project_id) && message.sender_id !== employee.id : message.recipient_id === employee.id);
  relevant.sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)));
  const folders = el("div", { class: "employee-filters", "aria-label": t("信箱分类", "Mailbox folders") });
  for (const [value, label] of [["inbox",t("收件箱", "Inbox")],["sent",t("发件箱", "Sent")],["group",t("项目群消息", "Project broadcasts")]]) {
    const control = button(label, null, { class: "small", attrs: { "aria-pressed": String(state.mailboxFolder === value) } });control.addEventListener("click", () => { state.mailboxFolder = value; render(); }); folders.append(control);
  }
  const list = el("section", { class: "mailbox-list", "aria-label": t("员工信件", "Employee correspondence") });
  if (!relevant.length) list.append(empty(t("这里还没有信件", "No messages here yet"), state.mailboxFolder === "group" ? t("显示该员工当前项目的全员消息；发给其他员工的定向消息不会混入。", "Shows broadcasts in current projects; direct messages to other employees are excluded.") : t("这是 v0.8 的项目通信记录，旧版 v0.7 信箱不会自动迁入。发送普通消息不会启动员工。", "This is v0.8 project correspondence. v0.7 mailboxes are not automatically imported. Ordinary messages do not start employees.")));
  relevant.forEach(message => {
    const item = state.data.projects.find(p => p.id === message.project_id);
    const open = button(t("打开项目对话", "Open project conversation"), null, { class: "small" });
    open.addEventListener("click", () => { selectProject(message.project_id); selectView("messages"); loadMessages(); });
    list.append(el("article", { class: "message-row", "data-message-id": message.id }, el("div", { class: "message-meta" }, el("strong", {}, message.sender?.name || t("你 · Human", "You · Human")), el("span", {}, "→ ", message.recipient?.name || t("项目全员", "Everyone")), el("time", {}, formatDate(message.created_at,true))), el("h2", {}, message.title), el("p", { class: "message-body" }, message.body), el("div", { class: "message-actions" }, el("span", { class: "muted" }, icon("folder"), item?.name || t("历史项目", "Historical project")), open)));
  });
  return [heading(`${employee.name} · ${navLabels.mailboxes}`, t("管理员查看 · 显示该员工相关的项目通信，不代表登录或代替它发信。", "Administrator view · Project correspondence for this employee. This does not sign in or send as the employee.")),
    el("div", { class: "mailbox-toolbar" }, selector, folders, el("span", { class: "muted small-text" }, t(`${relevant.length} 封信`, `${relevant.length} messages`))), list,
    el("p", { class: "directory-note" }, t("给员工发信：打开对应项目 → 项目消息 → 选择收件人。Agent 自己通过授权的项目工具读写；查看此页不标记已读或自动确认。", "To write: open the project → Messages → choose the recipient. Agents use authorized project tools. Viewing this page does not mark messages read or acknowledge them."))];
}
function renderMembers() {
  if (!project()) return [heading(navLabels.members), empty(t("为团队选择一个项目", "Choose a project for your team"), t("员工已在全局登记；创建项目后选择需要的成员。", "Employees are registered globally. Create a project and choose its members."), "new-project", t("创建项目", "Create project"), "users")];
  const people = projectEmployees();
  return [heading(navLabels.members, t("从已登记员工中选择成员。移出项目不会删除员工身份或历史。", "Choose from registered employees. Removing a member keeps their identity and history."), [button(t("选择已有员工", "Choose existing employees"), "add-member", { class: "primary", icon: "plus" }), button(t("发现更多员工", "Discover more employees"), "discover", { icon: "search" })]),
    people.length ? el("div", { class: "employee-grid" }, people.map((employee) => employeeRow(employee, { membership: true }))) : empty(t("把已有员工加入团队", "Add existing employees to your team"), t("选择登记过的员工加入此项目，然后通过消息沟通或派发工作任务。", "Choose registered employees for this project, then send messages or assign work."), "add-member", t("选择已有员工", "Choose existing employees"), "users"),
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
function resourceRow(resource, projectId) {
  const read = button(t("实时文件", "Live file"), null, { class: "small", icon: "document" });
  read.addEventListener("click", () => openResourceReader(projectId, resource));
  const versions = button(t("固定版本", "Versions"), null, { class: "small" });
  versions.addEventListener("click", () => openResourceVersions(projectId, resource));
  return el("article", { class: "resource-row" }, icon("document"), el("div", { class: "resource-row-main" }, el("h3", {}, resource.name), el("code", {}, resource.path), el("p", { class: "resource-kind" }, resource.kind), el("div", { class: "resource-row-actions" }, read, versions)));
}
async function openResourceReader(projectId, resource, version = null) {
  if (projectId !== state.projectId) return;
  const body = openForm(resource.name || t("查看资料", "View resource"), version ? t("查看已冻结的内容；当前文件的后续编辑不会改变这一版本。", "View frozen content. Later edits to the live file do not change this version.") : t("实时读取登记的文件，显示本次内容与校验值。", "Read the registered file live and inspect its contents and checksum."));
  if (!body) return;
  const output = el("div", { class: "resource-reader", role: "status" }, t("正在读取…", "Reading…"));
  body.append(output);
  const current = () => projectId === state.projectId && formDialog.open && body.isConnected;
  try {
    const result = version ? await api.readResourceVersion(projectId, resource.id, version.id) : await api.readResource(projectId, resource.id);
    if (!current()) return;
    if (typeof result.content !== "string") throw new ApiError("invalid_response");
    const content = result.content;
    const provenance = result.provenance || {};
    const raw = el("pre", { class: "resource-text", tabindex: "0", "aria-label": t("资料原文", "Resource source") }, content);
    const stage = el("div", { class: "resource-reader-stage" }, raw);
    const tools = el("div", { class: "resource-reader-actions" });
    if (version) {
      const back = button(t("返回版本列表", "Back to versions"), null, { class: "small" });
      back.addEventListener("click", () => { if (current()) openResourceVersions(projectId, resource); });
      tools.append(back);
    }
    const html = /\.html?$/i.test(resource.path || result.source?.path || "") || /^\s*(?:<!doctype html|<html[\s>])/i.test(content);
    if (html) {
      const preview = button(t("隔离预览", "Isolated preview"), null, { class: "small" });
      preview.setAttribute("aria-pressed", "false");
      preview.addEventListener("click", () => {
        if (!current()) return;
        const showingPreview = preview.getAttribute("aria-pressed") === "true";
        preview.setAttribute("aria-pressed", String(!showingPreview));
        preview.textContent = showingPreview ? t("隔离预览", "Isolated preview") : t("查看原文", "View source");
        if (showingPreview) stage.replaceChildren(raw);
        else {
          const frame = el("iframe", { class: "resource-html-preview", sandbox: "allow-scripts", referrerpolicy: "no-referrer", title: t("资料隔离预览", "Isolated resource preview") });
          // Opaque sandbox origin: no owner storage, credentials, forms or popups.
          // The preview response CSP blocks connections and external subresources.
          if (!result.preview_url?.startsWith("/workbench-preview/")) {
            showToast(t("隔离预览不可用，请查看或下载原文。", "Isolated preview unavailable. View or download the source."), true);
            preview.setAttribute("aria-pressed", "false");
            preview.textContent = t("隔离预览", "Isolated preview");
            return;
          }
          frame.src = result.preview_url;
          stage.replaceChildren(frame);
        }
      });
      tools.append(preview);
    }
    const download = button(t("下载原文", "Download source"), null, { class: "small", icon: "download" });
    download.addEventListener("click", () => {
      if (!current()) return;
      const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
      const link = el("a", { href: url, download: (resource.path || resource.name || "resource.txt").split(/[\\/]/).pop() || "resource.txt" });
      document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    tools.append(download);
    output.replaceChildren(...[provenance.redacted_since_capture ? el("p", { class: "field-hint" }, t("此版本返回时已额外脱敏，内容校验值与捕获时不同；请勿当作完全相同的副本。", "Additional redaction changed the returned checksum; this is not a byte-identical copy.")) : null, result.version ? el("p", { class: "field-hint" }, result.version.status === "approved" ? t("团队已确认版本", "Team-approved version") : result.version.status === "proposed" ? t("尚未确认的提案版本", "Proposed version, awaiting approval") : t("版本状态未确认", "Version status unconfirmed")) : null, el("p", { class: "resource-provenance" }, provenance.mode === "snapshot" ? t("固定版本", "Frozen version") : provenance.mode === "live" ? t("本次为实时读取", "Read live for this request") : t("读取方式未确认", "Read mode unconfirmed"), " · ", formatDate(result.version?.created_at || provenance.read_at, true)), el("code", { class: "resource-hash" }, `SHA-256: ${provenance.content_sha256 || t("未提供", "Not provided")}`), tools, html ? el("p", { class: "field-hint" }, t("预览允许内联脚本，阻止外部资源和网络连接。原文与下载不会执行脚本。", "Preview allows inline scripts and blocks external resources and network connections. Source view and download do not execute scripts.")) : null, stage].filter((item) => item !== null));
    formDialog.addEventListener("close", () => stage.replaceChildren(), { once: true });
  } catch (error) { if (current()) output.replaceChildren(el("p", { class: "form-error", role: "alert" }, errorText(error))); }
}
async function openResourceVersions(projectId, resource) {
  if (projectId !== state.projectId) return;
  const body = openForm(t(`${resource.name} · 固定版本`, `${resource.name} · Versions`), t("实时文件用于查看当前编辑；固定版本用于团队确认和追溯。冻结不代表已同步到其他设备。", "Live files show current edits. Frozen versions support team approval and traceability; freezing does not imply device synchronization."));
  if (!body) return;
  const summary = el("input", { type: "text", maxlength: 500, placeholder: t("本次版本摘要（可选）", "Version summary (optional)"), "aria-label": t("版本摘要", "Version summary") });
  const create = button(t("冻结当前文件", "Freeze current file"), null, { class: "small primary" });
  const list = el("div", { class: "version-list", role: "status" });
  const errorBox = el("p", { class: "form-error", role: "alert", hidden: true });
  body.append(el("div", { class: "version-toolbar" }, summary, create), errorBox, list);
  const current = () => projectId === state.projectId && formDialog.open && body.isConnected;
  const fail = (error) => { if (current()) { errorBox.hidden = false; errorBox.textContent = errorText(error); } };
  const load = async () => {
    list.textContent = t("正在读取版本…", "Loading versions…");
    try {
      const result = await api.resourceVersions(projectId, resource.id);
      if (!current()) return;
      if (!Array.isArray(result.versions)) throw new ApiError("invalid_response");
      list.replaceChildren(...result.versions.map((version) => {
        const status = version.status === "approved" ? t("已确认", "Approved") : version.status === "proposed" ? t("待确认提案", "Proposed") : t("状态未确认", "Status unconfirmed");
        const view = button(t("查看版本", "View version"), null, { class: "small" });
        view.addEventListener("click", () => { if (current()) openResourceReader(projectId, resource, version); });
        const actions = el("div", { class: "resource-reader-actions" }, view);
        if (version.status === "proposed") {
          const approve = button(t("确认此版本", "Approve version"), null, { class: "small" });
          approve.addEventListener("click", async () => {
            approve.disabled = true;
            try { await api.approveResourceVersion(projectId, resource.id, version.id); if (current()) await load(); }
            catch (error) { fail(error); if (current()) approve.disabled = false; }
          });
          actions.append(approve);
        }
        return el("article", { class: "resource-version" }, el("h3", {}, status, " · ", formatDate(version.created_at, true)), el("p", {}, version.summary || t("无摘要", "No summary")), el("p", { class: "field-hint" }, version.approved_at ? `${t("确认时间", "Approved at")}: ${formatDate(version.approved_at, true)} · ${version.approved_by || ""}` : `${t("创建者", "Created by")}: ${version.created_by || t("未提供", "Not provided")}`), el("code", { class: "resource-hash" }, `SHA-256: ${version.content_sha256 || t("未提供", "Not provided")}`), actions);
      }));
      if (!result.versions.length) list.append(el("p", { class: "inline-empty" }, t("尚无固定版本。确认当前文件后，冻结一份供团队引用。", "No frozen versions yet. Review the live file and freeze a version for the team to reference.")));
    } catch (error) { if (current()) list.replaceChildren(el("p", { class: "form-error", role: "alert" }, errorText(error))); }
  };
  create.addEventListener("click", async () => {
    create.disabled = true; errorBox.hidden = true;
    try {
      const result = await api.createResourceVersion(projectId, resource.id, summary.value.trim());
      if (!current()) return;
      summary.value = "";
      showToast(result.version?.status === "approved" || result.status === "approved" ? t("当前文件已冻结并确认。", "Current file frozen and approved.") : t("当前文件已冻结，请检查版本状态。", "Current file frozen. Check its version status."));
      await load();
    } catch (error) { fail(error); }
    finally { if (current()) create.disabled = false; }
  });
  await load();
}
function renderKnowledgePanel(projectId) {
  const status = el("div", { class: "knowledge-status", role: "status" }, t("可选工具，尚未检查。只在你点击时检查或查询。", "Optional tool, not checked yet. Checks and queries run only when you click."));
  const check = button(t("检查 CodeGraph", "Check CodeGraph"), null, { class: "small", icon: "search" });
  const question = el("textarea", { id: "knowledge-query", maxlength: 500, rows: 2, placeholder: t("输入函数、类或路由名称", "Enter a function, class, or route name") });
  const query = el("button", { class: "button small", type: "submit", disabled: true }, t("查询代码", "Query code"));
  const output = el("div", { class: "knowledge-output", role: "status" });
  const panel = el("section", { class: "knowledge-panel" });
  let ready = false;
  let busy = false;
  let sequence = 0;
  const current = () => projectId === state.projectId && panel.isConnected;
  const updateQuery = () => { query.disabled = busy || !ready || !question.value.trim() || question.value.length > 500; };
  question.addEventListener("input", updateQuery);
  check.addEventListener("click", async () => {
    const requestId = ++sequence;
    ready = false; busy = true; check.disabled = true; updateQuery(); output.replaceChildren();
    status.textContent = t("正在检查…", "Checking…");
    try {
      const result = await api.knowledgeStatus(projectId);
      if (!current() || requestId !== sequence) return;
      ready = result.available === true && result.index_ready === true;
      const reason = result.reason || result.error?.message || result.message || "";
      status.replaceChildren(el("p", {}, ready ? t("索引可查询；未核验当前源码", "Index query available; current source not verified") : !result.available ? t("CodeGraph 未安装或不可用；这是可选功能。", "CodeGraph is not installed or unavailable; it is optional.") : t("尚无可用索引。请在项目中准备索引后再次检查。", "No usable index. Prepare the project's index, then check again.")), reason ? el("p", { class: "muted" }, reason) : null, result.provider_version ? el("p", { class: "muted" }, `CodeGraph ${result.provider_version}`) : null, el("p", { class: "muted" }, result.freshness === "unknown" || !result.freshness ? t("新鲜度未知：未核验当前源码。", "Freshness unknown: current source not verified.") : `${t("索引状态", "Index freshness")}: ${typeof result.freshness === "string" ? result.freshness : JSON.stringify(result.freshness)}`));
    } catch (error) { if (current() && requestId === sequence) status.textContent = errorText(error); }
    finally { if (current() && requestId === sequence) { busy = false; check.disabled = false; updateQuery(); } }
  });
  const form = el("form", {}, formField(t("符号名称（最多 500 字符）", "Symbol name (up to 500 characters)"), question), query);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = question.value.trim();
    if (!ready || busy || !value || question.value.length > 500 || !current()) return;
    const requestId = ++sequence;
    busy = true; check.disabled = true; updateQuery(); output.textContent = t("正在查询…", "Querying…");
    try {
      const result = await api.queryKnowledge(projectId, value);
      if (!current() || requestId !== sequence) return;
      if (typeof result.content !== "string") throw new ApiError("invalid_response");
      const text = result.content.slice(0, 24000);
      output.replaceChildren(...[el("p", { class: "field-hint" }, t("索引符号快照；未核验当前源码。", "Indexed symbol snapshot; current source not verified.")), el("pre", { class: "knowledge-result", tabindex: "0" }, text || t("没有匹配结果。", "No matching results.")), result.content.length > text.length ? el("p", { class: "field-hint" }, t("结果较长，仅显示前 24,000 字符。", "Showing the first 24,000 characters.")) : null, result.provenance ? el("p", { class: "knowledge-provenance" }, `${t("来源", "Provenance")}: ${JSON.stringify(result.provenance).slice(0, 2000)}`) : null].filter((item) => item !== null));
    } catch (error) { if (current() && requestId === sequence) output.replaceChildren(el("p", { class: "form-error", role: "alert" }, errorText(error))); }
    finally { if (current() && requestId === sequence) { busy = false; check.disabled = false; updateQuery(); } }
  });
  panel.append(el("div", { class: "section-heading" }, el("h2", {}, t("CodeGraph 代码符号检索", "CodeGraph symbol search")), check), status, form, output);
  return panel;
}
function renderActivity() {
  if (!project()) return renderWelcome();
  const projectId = state.projectId;
  const list = el("div", { class: "activity-list", role: "status" }, t("正在读取日志…", "Loading work log…"));
  const nodes = [heading(t("工作日志", "Work log"), t("汇总已记录的任务、邮件、笔记和资料版本，不把打开页面计为完成。", "Recorded tasks, mail, notes and resource versions. Opening a page does not count as completed work.")), list];
  const current = () => projectId === state.projectId && state.view === "activity" && list.isConnected;
  api.projectActivity(projectId).then((result) => {
    if (!current()) return;
    if (!Array.isArray(result.events)) throw new ApiError("invalid_response");
    list.replaceChildren(...result.events.map((event) => {
      const rawActor = event.actor_name || event.actor;
      const actor = rawActor === "human" ? "Human" : rawActor === "system" ? t("系统", "System") : rawActor || t("执行者未提供", "Actor not provided");
      const source = event.type?.startsWith("task") ? t("任务", "Task") : event.type?.startsWith("message") ? t("邮件", "Mail") : event.type?.startsWith("resource") ? t("资料", "Resource") : event.type?.startsWith("memory") || event.type?.startsWith("note") ? t("笔记", "Note") : t("记录", "Record");
      const row = el("article", { class: "activity-row" }, el("div", { class: "activity-meta" }, el("span", {}, source), el("time", { datetime: event.created_at || "" }, formatDate(event.created_at, true))), el("h3", {}, event.title === event.type ? ({ resource_version_created: t("资料版本已冻结", "Resource version frozen"), resource_version_approved: t("资料版本已确认", "Resource version approved"), employee_joined: t("员工已加入项目", "Employee joined the project"), employee_removed: t("员工已移出项目", "Employee removed from the project"), employee_lifecycle: t("员工状态已变更", "Employee status changed") }[event.type] || event.title) : event.title || event.type), event.summary ? el("p", {}, event.summary) : null, el("p", { class: "field-hint" }, actor));
      if (event.task_id) {
        const task = button(t("查看任务", "View task"), null, { class: "small" });
        task.addEventListener("click", () => openTaskDetail(event.task_id));
        row.append(task);
      }
      return row;
    }));
    if (!result.events.length) list.append(el("p", { class: "inline-empty" }, t("还没有项目记录。派发任务、发送邮件或登记资料后，记录会出现在这里。", "No project records yet. Assign a task, send mail, or register resources to start the work log.")));
    if (result.has_older) list.append(el("p", { class: "field-hint" }, t("当前显示最近的记录；还有更早记录尚未显示。", "Showing recent records; older records are not displayed.")));
  }).catch((error) => { if (current()) list.replaceChildren(el("p", { class: "form-error", role: "alert" }, errorText(error))); });
  return nodes;
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
      resources.length ? resources.map((resource) => resourceRow(resource, state.projectId)) : el("p", { class: "inline-empty" }, t("添加需求、规则或参考资料的文本文件，供员工在任务中读取。", "Add text files with requirements, rules, or references for employees to read during tasks."))),
      el("section", {}, el("div", { class: "section-heading" }, el("h2", {}, t("项目记忆", "Project memory")), button(t("记录记忆", "Add memory"), "add-memory", { class: "small", icon: "plus" })), el("div", { class: "toolbar" }, search), memoryList)), renderKnowledgePanel(state.projectId)];
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
    const compatibility = (state.data.update_nodes || state.updates?.nodes || []).find(item => item.id === device.id);
    const revoke = !local && fleet?.listener && !device.revoked ? button(t("撤销配对", "Revoke pairing"), null, { class: "small" }) : null;
    revoke?.addEventListener("click", () => confirmFleetAction("revoke", device));
    return el("div", { class: "device-row" }, el("span", { class: "device-icon", "aria-hidden": "true" }, icon("monitor")), el("div", { class: "device-row-main" }, el("h2", {}, device.name), el("p", {}, local ? t("当前工作台所在设备", "This workbench's device") : t("已登记设备", "Registered device")), device.last_seen ? el("p", {}, `${t("最近连接", "Last seen")} · ${formatDate(device.last_seen, true)}`) : null, compatibility ? nodeCompatibility(compatibility) : null), tag(device.revoked ? "revoked" : device.status), revoke);
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
// Update metadata is fetched on entry or an explicit action, never on a timer.
async function loadUpdates({ force = false } = {}) {
  if (state.updatesLoading || state.updatesAction || (state.updatesAttempted && !force)) return;
  state.updatesAttempted = true;
  state.updatesLoading = true;
  state.updatesError = null;
  if (state.view === "about") render();
  try { state.updates = await api.updateStatus(); state.updatePauseUnconfirmed = false; }
  catch (error) { state.updatesError = error; }
  finally { state.updatesLoading = false; renderMaintenanceBanner(); if (state.view === "about") render(); }
}
async function updateAction(action, channel) {
  if (state.updatesAction) return;
  state.updatesAction = action;
  state.updatesError = null;
  if (state.view === "about") render();
  renderMaintenanceBanner();
  try {
    const result = await ({ check: api.checkUpdates, prepare: api.prepareUpdate, resume: api.resumeUpdates, channel: () => api.updateChannel(channel) })[action]();
    state.updates = result;
    state.updatePauseUnconfirmed = false;
    if (state.data) state.data.update_maintenance = result.maintenance;
  } catch (error) {
    state.updatesError = error;
    if (action === "prepare") state.updatePauseUnconfirmed = true;
    // Preparation can pause before returning an error. Reload authoritative state,
    // but retain the failed action's explanation and a usable resume control.
    try {
      state.updates = await api.updateStatus();
      state.updatePauseUnconfirmed = false;
      if (state.data) state.data.update_maintenance = state.updates.maintenance;
    } catch { await refresh({ silent: true }); }
  } finally {
    state.updatesAction = "";
    renderMaintenanceBanner();
    if (state.view === "about") render();
  }
}
function updateControl(label, action, primary = false) {
  const control = button(label, null, { class: `${primary ? "primary " : ""}small`, disabled: Boolean(state.updatesAction || state.updatesLoading) });
  control.addEventListener("click", () => updateAction(action));
  return control;
}
function renderMaintenanceBanner() {
  let banner = document.getElementById("update-maintenance-banner");
  if (!banner) {
    banner = el("div", { id: "update-maintenance-banner", class: "update-maintenance", role: "status", hidden: true });
    root.before(banner);
  }
  const maintenance = state.data?.update_maintenance || state.updates?.maintenance;
  banner.hidden = !maintenance?.paused || state.applicationStopped;
  if (banner.hidden) return;
  const details = button(t("查看更新准备", "View update preparation"), null, { class: "small" });
  details.addEventListener("click", () => { selectView("about"); loadUpdates({ force: true }); });
  banner.replaceChildren(icon("pause"), el("div", { class: "update-maintenance-copy" },
    el("strong", {}, t("更新准备中 · 已暂停领取新任务", "Update preparation · New task claims paused")),
    el("p", {}, t("已有任务继续执行，队列保留。重启后仍暂停，需要你显式恢复接单。", "Existing tasks continue and the queue is retained. The pause survives restart; explicitly resume task claims when ready."))),
    el("div", { class: "update-actions" }, details, updateControl(t("恢复接单", "Resume task claims"), "resume")));
}
function nodeCompatibility(node) {
  const labels = {
    matched: t("同版本 · 协议兼容", "Same version · Protocol compatible"),
    compatible: t("版本不同 · 协议兼容", "Different version · Protocol compatible"),
    unknown: t("版本或协议未确认", "Version or protocol unconfirmed"),
    incompatible: t("协议不兼容 · 不能领取新任务", "Protocol incompatible · New claims blocked"),
  };
  return el("span", { class: `update-node-status ${node.status === "incompatible" ? "error" : ""}` }, labels[node.status] || labels.unknown);
}
function formatUpdateBytes(value) {
  const bytes = Number(value) || 0;
  return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`;
}
function renderUpdateSection() {
  const data = state.updates;
  const busy = state.updatesLoading || Boolean(state.updatesAction);
  const maintenance = state.data?.update_maintenance || data?.maintenance;
  const paused = maintenance?.paused;
  const section = el("section", { class: "about-section update-section", "aria-busy": String(busy) }, el("h2", {}, t("版本与升级", "Version & updates")),
    el("p", {}, t("当前本地 Beta 尚未公开发布。只在你点击时检查 GitHub，不会自动下载、替换应用或恢复接单。", "This local Beta is not publicly released. GitHub is checked only when you click; the app does not automatically download, replace itself, or resume task claims.")));
  if (state.updatesError) {
    const retry = button(t("重新读取更新设置", "Reload update settings"), null, { class: "small", disabled: busy });
    retry.addEventListener("click", () => loadUpdates({ force: true }));
    section.append(el("p", { class: "update-error", role: "alert" }, errorText(state.updatesError)), retry);
  }
  if (!data) {
    const retry = button(t("重新读取更新设置", "Reload update settings"), null, { class: "small", disabled: busy });
    retry.addEventListener("click", () => loadUpdates({ force: true }));
    section.append(el("p", { class: "muted" }, busy ? t("正在读取更新设置…", "Loading update settings…") : t("更新设置暂时不可用。", "Update settings are unavailable.")), retry);
    if (paused || state.updatePauseUnconfirmed) section.append(updateControl(t("恢复接单", "Resume task claims"), "resume"));
    return section;
  }
  const channel = el("select", { id: "update-channel", disabled: busy, "aria-label": t("更新渠道", "Update channel") },
    el("option", { value: "stable" }, t("稳定版（推荐）", "Stable (recommended)")), el("option", { value: "beta" }, t("Beta 与稳定版", "Beta & stable")));
  channel.value = data.channel || "stable";
  channel.addEventListener("change", () => updateAction("channel", channel.value));
  section.append(el("div", { class: "update-toolbar" }, formField(t("更新渠道", "Update channel"), channel), updateControl(state.updatesAction === "check" ? t("正在检查…", "Checking…") : t("检查更新", "Check for updates"), "check")),
    el("p", { class: "muted" }, t("稳定版不含预发布；Beta 渠道也包含稳定版。切换渠道后请重新检查。", "Stable excludes prereleases. The Beta channel also includes stable releases. Check again after changing channels.")));
  const check = data.check;
  if (!check) section.append(el("p", { class: "update-check-result" }, t("尚未检查公开发行版。", "Public releases have not been checked.")));
  else {
    const labels = { update_available: t("发现新版本", "Update available"), current: t("当前版本无需更新", "No update needed for this version"), no_releases: t("此渠道暂无公开发行版", "No public releases in this channel"), error: t("检查失败，请重试", "Check failed; try again") };
    section.append(el("p", { class: "update-check-result", role: "status" }, labels[check.status] || t("检查结果待确认", "Check result unconfirmed"), check.checked_at ? ` · ${formatDate(check.checked_at, true)}` : ""));
    if (check.status === "error") section.append(el("p", { class: "update-error" }, check.error?.message || t("无法读取 GitHub 发行版。", "Could not read GitHub releases."), check.error?.code ? ` (${check.error.code})` : ""));
    if (check.latest && check.status !== "error") {
      const url = String(check.latest.url || "");
      const safe = /^https:\/\/github\.com\/polaris-smart\/agent-mailbox\/releases(?:\/|$)/.test(url);
      section.append(el("p", {}, t("公开发行版", "Public release"), " · ", el("strong", {}, check.latest.version || ""), safe ? el("a", { class: "update-release-link", href: url, target: "_blank", rel: "noopener noreferrer" }, t("查看发行说明 ↗", "Open release notes ↗")) : null));
      if (check.latest.notes) section.append(el("details", { class: "update-release-notes" }, el("summary", {}, t("发行说明", "Release notes")), el("pre", {}, String(check.latest.notes).slice(0, 12000))));
    }
  }
  const kind = { app: t("桌面应用", "Desktop app"), wheel: t("Python 安装包", "Python package"), source: t("源码运行", "Source checkout"), unknown: t("安装方式未确认", "Installation type unconfirmed") };
  section.append(el("h3", {}, t("当前安装与操作", "Installation & next steps")), el("p", {}, kind[data.installation?.kind] || kind.unknown, " · ", ({ darwin: "macOS", Darwin: "macOS", win32: "Windows", linux: "Linux" })[data.installation?.platform] || data.installation?.platform || "", " / ", data.installation?.architecture || ""));
  const instructions = data.installation?.instructions || [];
  if (instructions.length) section.append(el("ol", {}, instructions.map(item => el("li", {}, english ? item.en || item.zh : item.zh || item.en))));
  if (data.installation?.home) section.append(el("p", { class: "update-location" }, t("数据目录", "Data directory"), " · ", el("code", {}, data.installation.home)));
  section.append(el("h3", {}, t("安全更新准备", "Prepare safely")), el("p", {}, t("先暂停领取新任务，等待已有执行及远端回执结束，再备份工作台数据和身份配置。项目文件及供应商外部登录不在此备份中。准备失败也保持暂停。", "Pause new task claims, wait for existing executions and remote receipts, then back up workbench data and identity settings. Project files and external provider sign-in are excluded. Preparation failures keep claims paused.")));
  if (state.updatePauseUnconfirmed) section.append(el("p", { class: "update-error", role: "status" }, t("准备请求未完成，暂停状态暂时无法确认。重新读取设置或显式恢复接单；不要据此认为已完成备份。", "The preparation request did not complete and pause status cannot be confirmed. Reload settings or explicitly resume task claims; this does not confirm a completed backup.")));
  const active = maintenance?.active_tasks || [];
  if (paused) section.append(el("p", { class: "update-pause-state" }, t("已暂停接单", "Task claims paused"), ` · ${t("队列保留", "Queue retained")}: ${maintenance.queued_count || 0}`));
  const pendingClaims = maintenance?.pending_claims || [];
  if (paused && (active.length || pendingClaims.length || data.remote_pending?.length || data.status === "waiting")) section.append(el("p", { role: "status" }, t("等待现有任务、未确认的远端领取或执行回执。确认结束后点击“重新检查并备份”。", "Waiting for existing tasks, unconfirmed remote claims, or execution receipts. Once confirmed finished, click “Recheck & back up”.")),
    active.length ? el("ul", { class: "update-active-tasks" }, active.map(task => el("li", {}, task.title || task.id, " · ", statusLabels[task.status] || task.status))) : null);
  if (paused && data.status === "ready") section.append(el("p", { class: "update-pause-state", role: "status" }, t("更新准备已完成，接单仍暂停。请按当前安装方式升级并验证数据。", "Update preparation is complete; task claims remain paused. Upgrade for your installation type and verify your data.")));
  const backup = data.backup || data.last_backup;
  if (backup) section.append(el("div", { class: "update-backup" }, el("strong", {}, backup.verified ? t("私有备份已校验", "Private backup verified") : t("备份待校验", "Backup verification pending")),
    el("p", {}, `${formatDate(backup.created_at, true)} · ${backup.files ?? "—"} ${t("文件", "files")} · ${formatUpdateBytes(backup.bytes || 0)}`),
    el("code", {}, backup.path || ""), el("p", { class: "muted" }, t("备份保存在本机，不会通过页面下载身份凭证。升级后检查员工和项目，确认后恢复接单。", "The backup stays on this machine; identity credentials are not downloadable through this page. After upgrading, verify employees and projects, then resume task claims."))));
  section.append(el("div", { class: "update-actions" }, updateControl(state.updatesAction === "prepare" ? t("正在准备…", "Preparing…") : paused ? t("重新检查并备份", "Recheck & back up") : t("暂停接单并准备更新", "Pause claims & prepare update"), "prepare", true),
    paused || state.updatePauseUnconfirmed ? updateControl(t("恢复接单", "Resume task claims"), "resume") : null));
  if (data.nodes?.length) section.append(el("h3", {}, t("设备版本兼容性", "Device version compatibility")), el("p", { class: "muted" }, t("版本由节点报告。未报告的旧节点显示待确认，不代表兼容；协议不兼容时阻止新派工。", "Versions are reported by nodes. Older nodes without a report remain unconfirmed, not compatible; incompatible protocols block new claims.")),
    el("ul", { class: "update-node-list" }, data.nodes.map(node => el("li", {}, el("div", {}, el("strong", {}, node.name || node.id), node.is_local ? el("span", { class: "muted" }, " · ", t("本机", "This device")) : null, el("p", {}, node.version || t("版本未知", "Unknown version"), " · ", t("协议", "Protocol"), " ", node.protocol ?? "?")), nodeCompatibility(node)))));
  section.append(el("p", { class: "about-links" }, el("a", { href: "https://github.com/polaris-smart/agent-mailbox/releases", target: "_blank", rel: "noopener noreferrer" }, t("GitHub 发行页 ↗", "GitHub releases ↗")), el("a", { href: "https://github.com/polaris-smart/agent-mailbox/issues", target: "_blank", rel: "noopener noreferrer" }, t("反馈问题 ↗", "Report an issue ↗"))));
  return section;
}

function renderAbout() {
  const language = el("select", { id: "language-preference", "aria-label": t("界面语言", "Interface language") },
    el("option", { value: "auto" }, t("跟随浏览器", "Follow browser")),
    el("option", { value: "zh-CN" }, "简体中文"), el("option", { value: "en" }, "English"));
  language.value = languagePreference;
  language.addEventListener("change", () => {
    try {
      localStorage.setItem("agent-mailbox.workbench.language", language.value);
      sessionStorage.setItem("agent-mailbox.workbench.language-view", "about");
      location.reload();
    } catch { showToast(t("浏览器无法保存语言偏好，请检查存储权限。", "The browser cannot save your language preference. Check storage permissions."), true); }
  });
  const link = (label, url) => el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label);
  const section = (title, ...content) => el("section", { class: "about-section" }, el("h2", {}, title), ...content);
  const version = state.data?.version ? `v${state.data.version}` : t("版本未知", "Version unavailable");
  const licenses = [["LICENSE.txt", "Apache-2.0"], ["NOTICE.txt", "NOTICE"], ["MIT-Legacy.txt", t("原 MIT 版权声明", "Legacy MIT notices")]].map(([filename, label]) => {
    const pre = el("pre", { class: "license-text", tabindex: "0" });
    const details = el("details", { class: "license-document" }, el("summary", {}, label),
      link(t("打开原始文本 ↗", "Open original text ↗"), `/workbench-assets/${filename}`), pre);
    details.addEventListener("toggle", async () => {
      if (!details.open) return;
      if (state.legalDocuments[filename]) { pre.textContent = state.legalDocuments[filename]; return; }
      pre.textContent = t("正在读取…", "Loading…");
      try {
        const response = await fetch(`/workbench-assets/${filename}`, { cache: "no-store" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        state.legalDocuments[filename] = await response.text();
        pre.textContent = state.legalDocuments[filename];
      } catch { pre.textContent = t("读取失败。可重新展开重试，或打开原始文本。", "Could not load. Reopen to retry, or open the original text."); }
    });
    return details;
  });
  return [heading(t("关于与更新", "About & updates"), "agent-mailbox · " + version), el("div", { class: "about-content" },
    section(t("界面语言", "Interface language"), el("p", {}, t("支持简体中文与英文。偏好保存在当前浏览器；切换会重新加载界面，不会改变任务或项目资料的语言。", "Simplified Chinese and English are available. Preferences are stored in this browser. Switching reloads the interface and does not translate tasks or project content.")), language),
    renderUpdateSection(),
    section(t("开源与版权", "Open source & notices"), el("p", {}, "© 2026 NoFox · Apache-2.0"), el("p", {}, t("保留原有代码及第三方要求的版权声明。以下是随本版本发行的原始内容。", "Original and required third-party copyright notices are retained. The documents below ship with this version.")), ...licenses))];
}

function render() {
  renderSidebar();
  renderMaintenanceBanner();
  if (state.applicationStopped) {
    root.replaceChildren(heading(t("应用已退出", "Application closed"), t("任务、成果和项目记录仍然保留。", "Tasks, deliverables, and project records are retained.")), empty(t("重新启动 Agent Mailbox", "Restart Agent Mailbox"), t("可以关闭这个浏览器页面。使用原来的应用入口或启动命令重新启动，再打开它提供的新地址。", "You can close this browser page. Restart using your application launcher or original command, then open its new address."), null, null, "monitor"));
    root.setAttribute("aria-busy", "false");
    return;
  }
  const renders = { overview: renderOverview, tasks: renderTasks, employees: renderEmployees, mailboxes: renderMailboxes, members: renderMembers, messages: renderMessages, resources: renderResources, activity: renderActivity, devices: renderDevices, about: renderAbout };
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
    if (result.update_maintenance) { state.updatePauseUnconfirmed = false; if (state.updates) state.updates.maintenance = result.update_maintenance; }
    renderMaintenanceBanner();
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
    if (state.view === "about") loadUpdates();
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
    el("label", { class: "radio-choice" }, write, el("div", {}, el("strong", {}, t("可修改项目文件", "Project write access")), el("p", {}, t("在独立 Git 工作区编辑。原仓库需有提交且干净（含未跟踪文件）；验收不会自动合入。工作区不是系统安全沙箱。", "Edit in an independent Git worktree. The repository must have a commit and be clean, including untracked files. Acceptance does not apply changes. A worktree is not an OS sandbox.")))));
  const box = errorBox();
  const form = el("form", {}, formField(t("任务名称", "Task title"), title), formField(t("交给谁", "Assign to"), assignee), el("div", { class: "form-field" }, el("label", { for: model.id }, t("模型（可选）", "Model (optional)")), model, modelHint), formField(t("工作说明", "Instructions"), prompt), el("div", { class: "form-field" }, el("span", { class: "field-hint" }, t("这次任务的权限", "Permissions for this task")), permissions), box);
  const end = footer(t("派发任务", "Assign task"));
  const runtimeBox = el("div", { id: "task-runtime-status", role: "status" });
  const isRemote = () => { const employee = people.find((item) => item.id === assignee.value); return Boolean(employee?.node_id && employee.node_id !== state.data.node?.id); };
  const canAssign = () => isRemote() || Boolean(state.data.runtime?.installed && state.data.runtime?.node_available);
  const updateRuntime = () => {
    if (!formDialog.open || !body.isConnected) return;
    write.disabled = isRemote();
    if (write.disabled) readOnly.checked = true;
    end.submit.disabled = !canAssign() || Boolean(end.submit.dataset.loading);
    if (isRemote()) runtimeBox.replaceChildren(el("p", { class: "field-hint" }, t("这项工作在员工所在设备执行。远端暂仅支持只读任务，尚不支持独立修改工作区和合入。", "Work runs on the employee's device. Remote tasks currently support read-only work; isolated editing and applying changes are unavailable.")));
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
    const [result, deliveryResult] = await Promise.all([api.taskDetail(taskId), api.taskDelivery(taskId).catch(error => ({ load_error: errorText(error) }))]);
    result.delivery = deliveryResult.delivery || deliveryResult;
    if (requestId !== state.detailRequest || taskId !== state.detailTaskId || !detailDialog.open) return;
    if (!result.task || !Array.isArray(result.events) || !Array.isArray(result.permissions)) throw new ApiError("invalid_response");
    state.detailError = "";
    const fingerprint = JSON.stringify(result);
    if (silent && fingerprint === state.detailFingerprint) return;
    // Do not interrupt a human writing review notes with a polling refresh.
    if (silent && detailContent.contains(document.activeElement) && ["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) return;
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
function deliveryPanel(task, delivery, box) {
  const panel = el("section", { class: "detail-section delivery-panel" }, el("h3", {}, t("交付与代码变更", "Delivery & code changes")));
  if (!delivery) { panel.append(el("p", {}, t("尚无交付记录。", "No delivery recorded yet."))); return panel; }
  if (delivery.load_error) { panel.append(el("p", { class: "permission-expired" }, t("无法读取交付：", "Could not load delivery: "), delivery.load_error)); return panel; }
  const workspace = delivery.workspace;
  if (workspace) panel.append(el("dl", { class: "delivery-facts" },
    el("dt", {}, t("原项目目录", "Original project")), el("dd", {}, el("code", {}, workspace.source_path)),
    el("dt", {}, t("独立工作区", "Isolated workspace")), el("dd", {}, el("code", {}, workspace.path)),
    el("dt", {}, t("起始提交", "Base commit")), el("dd", {}, el("code", {}, workspace.base_commit))));
  else panel.append(el("p", { class: "field-hint" }, t("此任务没有独立修改工作区。", "This task has no isolated editing workspace.")));
  if (delivery.capture_error) panel.append(el("p", { class: "permission-expired" }, t("交付捕获未完成：", "Delivery capture incomplete: "), typeof delivery.capture_error === "string" ? delivery.capture_error : delivery.capture_error.message || delivery.capture_error.code));
  if (delivery.summary && delivery.summary !== task.result) panel.append(el("p", { class: "prose" }, delivery.summary));
  if (delivery.files?.length) panel.append(el("ul", { class: "delivery-files" }, delivery.files.map(file => el("li", {}, el("code", {}, typeof file === "string" ? file : file.path || file.name || ""), typeof file === "object" && file.status ? ` · ${file.status}` : ""))));
  if (delivery.diff) panel.append(el("details", { class: "operation-details" }, el("summary", {}, t("查看代码差异", "View code diff")), el("pre", { class: "delivery-diff" }, el("code", {}, delivery.diff))));
  panel.append(el("p", { class: "field-hint" }, t("员工报告的验证结果不等于系统独立验证。", "Employee-reported verification is not independent system verification.")));
  if (delivery.system?.tests_run === false) panel.append(el("p", { class: "field-hint" }, t("系统未独立运行测试。请查看员工的验证记录与交付内容后决定验收。", "The system did not independently run tests. Review the employee's evidence and delivery before accepting.")));
  else if (delivery.verification?.notes) panel.append(el("p", { class: "prose" }, Array.isArray(delivery.verification.notes) ? delivery.verification.notes.join("\n") : delivery.verification.notes));

  if (delivery.applied) panel.append(el("p", {}, t("代码变更已应用到原项目目录，尚不代表已提交或推送。", "Changes were applied to the original project. This does not mean they were committed or pushed.")));
  else if (task.status === "done" && delivery.can_apply) {
    const confirm = el("input", { type: "checkbox", id: "delivery-apply-confirm" });
    const apply = button(t("将变更应用到原项目", "Apply changes to original project"), null, { class: "primary", disabled: true });
    panel.append(el("label", { class: "apply-confirm" }, confirm, t("我确认将此固定交付应用到原目录；系统不会自动提交或推送。", "I confirm applying this captured delivery to the original directory. Nothing is committed or pushed automatically.")), apply);
    confirm.addEventListener("change", () => { apply.disabled = !confirm.checked; });
    apply.addEventListener("click", () => { if (!confirm.checked) return; submitAction(apply, box, () => api.applyDelivery(task.id), async () => { await loadTaskDetail(); await refresh({ silent: true }); }); });
  } else if (workspace && !delivery.applied) panel.append(el("p", { class: "field-hint" }, task.status !== "done" ? t("人工验收通过后，才能另外确认合入。", "After accepting the work, you can separately confirm applying changes.") : ({ no_changes: t("此交付没有文件变更，无需合入。", "This delivery has no file changes to apply."), unsafe_or_no_workspace: t("交付未通过安全检查或没有独立工作区，不能自动合入。", "The delivery has no safe captured patch or isolated workspace; automatic application is unavailable."), delivery_integrity_error: t("交付校验失败，不能合入。", "Delivery integrity validation failed. Changes cannot be applied.") })[delivery.apply_blocked_reason] || t("当前交付不能自动应用，请检查基线和原仓库状态。", "This delivery cannot be applied automatically. Check the base commit and original repository state.")));
  return panel;
}

function renderTaskDetail({ task, events, permissions, delivery, links = [] }) {
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
  content.append(deliveryPanel(task, delivery, box));
  if (links.length) content.append(el("section", { class: "detail-section" }, el("h3", {}, t("关联任务", "Related tasks")), links.map(link => {
    const control = button(`${link.direction === "parent" ? t("上一轮", "Previous task") : t("后续任务", "Follow-up task")} · ${link.title || link.task_id}`, null, { class: "text-button" });
    control.addEventListener("click", () => openTaskDetail(link.task_id)); return control;
  })));
  if (task.result) content.append(el("section", { class: "detail-section" }, el("h3", {}, t("员工交付", "Employee output")), el("div", { class: "result-block" }, el("p", { class: "prose" }, typeof task.result === "string" ? task.result : JSON.stringify(task.result, null, 2)))));
  if (task.error) content.append(el("section", { class: "detail-section" }, el("h3", {}, t("需要处理的问题", "Issue to resolve")), el("p", { class: "prose" }, typeof task.error === "string" ? task.error : task.error.message || JSON.stringify(task.error))));
  if (task.error?.code === "MODEL_UNSUPPORTED") content.append(el("p", { class: "field-hint" }, t("这个模型不适用于员工当前的登录方式。新建任务时，从模型列表选择服务实际提供的模型；原任务保留为失败，不会自动换模型重跑。", "This model is not supported by the employee's current sign-in method. Create a new task and select a model advertised by the service. The failed task is retained and is not automatically retried with another model.")));
  if (task.error) content.append(executionGuide(state.data.employees.find(item => item.id === task.assignee_id), task.error));
  if (task.status === "review") {
    const note = el("textarea", { id: "review-note", placeholder: t("可选：写下验收意见；退回时说明还需要完成什么。", "Optional review note. When returning work, explain what still needs to be done."), "aria-label": t("验收意见", "Review note") });
    const accept = button(t("通过验收", "Accept work"), null, { class: "primary", icon: "check" });
    const reject = button(t("退回补充", "Return for follow-up"), null);
    accept.removeAttribute("data-action"); reject.removeAttribute("data-action");
    const review = (control, decision) => submitAction(control, box, () => api.reviewTask(task.id, { decision, note: note.value.trim() }), async () => {
      showToast(decision === "accept" ? t("任务已通过验收。", "Task accepted.") : t("任务已退回，后续状态以任务记录为准。", "Task returned. Check the task record for its next status."));
      await loadTaskDetail(); await refresh({ silent: true });
    });
    accept.addEventListener("click", () => review(accept, "accept"));
    reject.addEventListener("click", () => {
      if (!note.value.trim()) { formError(box, new ApiError(t("请写明还需要补充什么。", "Describe what needs to be changed."))); note.focus(); return; }
      submitAction(reject, box, () => api.followUpTask(task.id, note.value.trim()), async result => {
        showToast(t("原任务已退回，后续任务保留上一轮交付与修改要求。", "Work returned. The follow-up retains the previous delivery and your instructions."));
        await refresh({ silent: true });
        const nextId = result.task?.id || result.task_id;
        if (nextId) await openTaskDetail(nextId); else await loadTaskDetail();
      });
    });
    content.append(el("section", { class: "review-panel" }, el("h3", {}, t("成果已交付，等你验收", "The work is ready for your review")), el("p", {}, t("通过验收不会合入代码。退回时请写明要求，系统会创建保留上下文的后续任务。", "Acceptance does not apply code changes. Returning work requires instructions and creates a follow-up task with context.")), note, el("div", { class: "approval-actions" }, accept, reject)));
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
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSidebar(false);
  if (event.key !== "Tab" || formDialog.open || detailDialog.open || !document.body.classList.contains("drawer-open")) return;
  const controls = [...document.getElementById("sidebar").querySelectorAll('a[href], button:not(:disabled), summary, [tabindex="0"]')].filter((node) => node.getClientRects().length);
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});
matchMedia("(max-width: 760px)").addEventListener("change", () => setSidebar(false));
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
document.getElementById("version-label").setAttribute("aria-label", t("关于与更新", "About & updates"));
document.querySelector(".brand small").textContent = t("团队工作台", "Team workbench");
document.getElementById("project-nav-label").textContent = t("当前项目", "Current project");
document.getElementById("team-nav-label").textContent = t("团队", "Team");
document.getElementById("manage-nav-label").textContent = t("管理", "Manage");
document.getElementById("sidebar").setAttribute("aria-label", t("团队与项目导航", "Team and project navigation"));
document.querySelector(".global-nav").setAttribute("aria-label", t("团队", "Team"));
document.querySelector(".project-nav").setAttribute("aria-label", t("当前项目工作区", "Current project workspace"));
document.getElementById("project-list").setAttribute("aria-label", t("项目列表", "Projects"));
document.getElementById("sidebar-toggle").setAttribute("aria-label", t("打开导航", "Open navigation"));
document.getElementById("sidebar-backdrop").setAttribute("aria-label", t("关闭导航", "Close navigation"));
document.querySelector(".brand").setAttribute("aria-label", t("agent-mailbox 工作台", "agent-mailbox workbench"));
document.querySelector('[data-action="new-project"]').title = t("新建项目", "New project");
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
