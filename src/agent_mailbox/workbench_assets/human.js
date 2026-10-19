const $ = (id) => document.getElementById(id);
const humanKey = "agent-mailbox.human.token";
const ownerKey = "agent-mailbox.workbench.token";
const profileKey = "agent-mailbox.human.profile-epoch";
const tabEpochKey = "agent-mailbox.human.tab-epoch";
const profileStorageError = "此浏览器禁止站点共享存储，无法安全切换 Human 身份。请允许本站点存储，或使用独立浏览器资料。";
let token = "";
let humanId = "";
let spaces = [];
let projects = [];
let selectedProjectId = "";
let generation = 0;
let accessController = null;
let profileEpoch = "";
let profileChannel = null;
try { token = sessionStorage.getItem(humanKey) || ""; } catch { /* tab storage unavailable */ }
try { profileEpoch = sessionStorage.getItem(tabEpochKey) || ""; } catch { /* unavailable */ }
try { profileChannel = new BroadcastChannel("agent-mailbox.human.profile"); }
catch { /* storage event and visible-tab check remain available */ }
try {
  const ownerAvailable = Boolean(sessionStorage.getItem(ownerKey));
  $("owner-entry").hidden = !ownerAvailable;
  $("owner-workbench").hidden = !ownerAvailable;
} catch { /* no owner session */ }

function notice(message, error = false) {
  const box = $("notice");
  box.textContent = message;
  box.classList.toggle("error", error);
  box.hidden = !message;
}

async function request(path, method = "GET", body, bearer = token) {
  const response = await fetch("/api/workbench/" + path, {
    method,
    headers: { "Accept": "application/json", "Authorization": "Bearer " + bearer,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin", cache: "no-store",
  });
  let result;
  try { result = await response.json(); } catch { throw new Error("服务返回了无法读取的响应。请稍后重试。"); }
  if (!response.ok) {
    const problem = new Error(result?.error?.message || "请求未完成。请稍后重试。");
    problem.status = response.status;
    throw problem;
  }
  return result;
}

function clearSession() {
  stopAccessWatch();
  token = "";
  humanId = "";
  profileEpoch = "";
  try {
    sessionStorage.removeItem(humanKey);
    sessionStorage.removeItem(tabEpochKey);
  } catch { /* memory session only */ }
  $("logout").hidden = true;
  suspendPrivateView();
}

function sharedProfileEpoch() {
  try { return localStorage.getItem(profileKey); }
  catch { return undefined; }
}

function requireProfileStorage() {
  const probe = profileKey + ".probe";
  try {
    localStorage.setItem(probe, "ready");
    const ready = localStorage.getItem(probe) === "ready";
    localStorage.removeItem(probe);
    if (ready) return;
  } catch { /* fail closed before issuing or restoring a Human session */ }
  throw new Error(profileStorageError);
}

function changeProfileEpoch(keepThisSession) {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const next = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  if (keepThisSession) requireProfileStorage();
  try {
    localStorage.setItem(profileKey, next);
    if (keepThisSession && localStorage.getItem(profileKey) !== next) throw new Error(profileStorageError);
  } catch (error) {
    if (keepThisSession) throw new Error(profileStorageError, { cause: error });
    // Logout already revoked this server session; other tabs fail closed on focus.
  }
  if (keepThisSession) {
    profileEpoch = next;
    try { sessionStorage.setItem(tabEpochKey, next); } catch { /* memory only */ }
  }
  profileChannel?.postMessage({ epoch: next });
}

function profileIsCurrent() {
  const shared = sharedProfileEpoch();
  return Boolean(profileEpoch) && shared === profileEpoch;
}

function clearOldProfile(message) {
  let ownerCleared = true;
  try { leaveOwnerInThisTab(); }
  catch { ownerCleared = false; }
  clearSession();
  notice(ownerCleared ? message :
    "无法清除本标签中的管理者会话。请关闭此标签后再使用其他 Human 身份。", true);
}

function leaveOldProfile() {
  if (!token || profileIsCurrent()) return false;
  const unavailable = sharedProfileEpoch() === undefined;
  clearOldProfile(unavailable ? profileStorageError :
    "此浏览器已切换到另一位 Human。当前标签的旧内容已清除。");
  return true;
}

if (profileChannel) {
  profileChannel.onmessage = (event) => {
    if (token && event.data?.epoch && event.data.epoch !== profileEpoch) {
      clearOldProfile("此浏览器已切换到另一位 Human。当前标签的旧内容已清除。");
    }
  };
}
window.addEventListener("storage", (event) => {
  if (event.key === profileKey) leaveOldProfile();
});

function stopAccessWatch() {
  accessController?.abort();
  accessController = null;
}

function clearReviewDialog() {
  const dialog = $("review-dialog");
  if (dialog.open) dialog.close();
  dialog.dataset.route = "";
  $("review-title").textContent = "验收交付";
  $("review-verification").textContent = "";
  $("review-summary").textContent = "";
  $("review-diff").textContent = "";
  $("review-note").value = "";
  $("review-checked").checked = false;
}

function suspendPrivateView(preserveRecoveryReceipt = false) {
  generation += 1;
  clearReviewDialog();
  // Access-stream loss must scrub the same private dialogs as logout. The
  // session token survives for an explicit recheck, but its old content does not.
  for (const [dialog, fields] of [
    ["invitation-receipt", ["invitation-human", "invitation-code"]],
    ["recovery-receipt", ["receipt-human", "receipt-code"]],
    ["member-removal-confirm", ["member-removal-name"]],
    ["reviewer-confirm", ["reviewer-confirm-description"]],
    ["fleet-device-revoke", ["fleet-device-revoke-name"]],
    ["fleet-leave-confirm", []],
  ]) {
    if (preserveRecoveryReceipt && dialog === "recovery-receipt") continue;
    const element = $(dialog);
    if (dialog === "fleet-leave-confirm") element.returnValue = "";
    if (element.open) element.close();
    for (const field of fields) $(field).textContent = "";
  }
  if ($("fleet-invite-receipt").open) $("fleet-invite-receipt").close();
  $("fleet-invite-code").value = "";
  $("fleet-join-invite").value = "";
  $("fleet-map-path").value = "";
  $("fleet-employee-name").value = "";
  $("fleet-relocate-url").value = "";
  $("fleet-discovery-results").replaceChildren();
  for (const id of ["human-forward-error", "human-forward-command", "fleet-listener-state",
    "fleet-ssh-guide", "fleet-join-state", "fleet-map-state", "fleet-employee-state"]) {
    $(id).textContent = "";
  }
  for (const id of ["human-forward-tools", "fleet-listener-tools", "fleet-join-tools",
    "fleet-relocate-form", "fleet-map-form", "fleet-employee-form", "fleet-leave-open",
    "human-forward-error", "human-forward-command", "fleet-ssh-guide"]) {
    $(id).hidden = true;
  }
  $("fleet-map-project").replaceChildren();
  $("fleet-employee-kind").replaceChildren();
  $("fleet-listener-address").value = "127.0.0.1";
  $("fleet-listener-address").disabled = false;
  for (const id of ["invitation", "recovery-human", "recovery-code", "new-project-name",
    "new-space-name", "new-human-name"]) $(id).value = "";
  spaces = [];
  projects = [];
  selectedProjectId = "";
  $("identity").textContent = "";
  $("identity").hidden = true;
  $("spaces").replaceChildren();
  $("projects").replaceChildren();
  $("space-count").textContent = "";
  $("project-count").textContent = "";
  $("detail-title").textContent = "选择一个项目";
  $("detail-scope").hidden = true;
  $("detail-description").textContent = "";
  $("detail-body").replaceChildren();
  $("new-human-result").textContent = "";
  $("new-human-result").hidden = true;
  $("invite-human-id").value = "";
  $("invite-tools").hidden = true;
  $("workspace").hidden = true;
  $("entry").hidden = false;
  $("recheck-access").hidden = !token;
}

async function watchAccess() {
  if (!token || accessController) return;
  const bearer = token;
  const controller = new AbortController();
  accessController = controller;
  try {
    const response = await fetch("/api/workbench/human/changes", {
      headers: { "Accept": "application/x-ndjson", "Authorization": "Bearer " + bearer },
      credentials: "same-origin", cache: "no-store", signal: controller.signal,
    });
    if (response.status === 401) {
      if (token === bearer) clearSession();
      return;
    }
    if (!response.ok || !response.body) throw new Error("访问变更通道不可用。");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    while (token === bearer && !controller.signal.aborted) {
      const { value, done } = await reader.read();
      if (done) throw new Error("访问变更通道已断开。");
      pending += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, boundary);
        pending = pending.slice(boundary + 1);
        if (!line) continue;
        const event = JSON.parse(line);
        if (token !== bearer || controller.signal.aborted) return;
        if (event.revoked) {
          clearSession();
          notice("当前会话已被撤销，请重新进入。", true);
          return;
        }
        if (event.access_changed || event.tasks_changed || event.messages_changed || event.resources_changed) {
          clearReviewDialog();
          await refresh();
        }
      }
    }
  } catch (error) {
    if (!controller.signal.aborted && token === bearer) {
      if (error.status === 401) clearSession();
      else suspendPrivateView();
      notice("无法确认当前访问权限。" + error.message + "请重新验证。", true);
    }
  } finally {
    if (accessController === controller) accessController = null;
  }
}

function leaveOwnerInThisTab() {
  let priorOwner = "";
  try { priorOwner = sessionStorage.getItem(ownerKey) || ""; }
  catch { throw new Error("无法确认本标签中的管理者会话。请允许本站点使用标签页存储后重试，或改用独立浏览器资料。"); }
  if (priorOwner) {
    try {
      sessionStorage.removeItem(ownerKey);
      if (sessionStorage.getItem(ownerKey)) throw new Error("owner token remains");
    } catch {
      throw new Error("无法清除本标签中的管理者会话。请在独立浏览器标签中进入。");
    }
  }
  $("owner-entry").hidden = true;
  $("owner-workbench").hidden = true;
  $("invite-tools").hidden = true;
}

function showReceipt(id, code) {
  if (!code) return;
  $("receipt-human").textContent = id;
  $("receipt-code").textContent = code;
  $("recovery-receipt").showModal();
  $("receipt-close").focus();
}

function showInvitation(id, code) {
  $("invitation-human").textContent = id;
  $("invitation-code").textContent = code;
  $("invitation-receipt").showModal();
  $("invitation-close").focus();
}

async function showReview(projectId, taskId) {
  const turn = generation;
  const route = "human/projects/" + encodeURIComponent(projectId) +
    "/tasks/" + encodeURIComponent(taskId) + "/review";
  const material = await request(route);
  if (turn !== generation || !token) return;
  const dialog = $("review-dialog");
  dialog.dataset.route = route;
  $("review-title").textContent = "验收交付 · " + material.title;
  const verification = material.verification || {};
  const statusLabel = {
    employee_report: "员工提交的文字报告，未经系统独立验证",
    not_verified: "尚未独立验证",
  }[verification.status] || "验证状态待核实";
  $("review-verification").textContent = "验证状态：" + statusLabel +
    (verification.notes ? " · " + verification.notes : "");
  $("review-summary").textContent = material.summary || "未提交文字摘要。";
  const files = (material.files || []).map((file) =>
    typeof file === "string" ? file : [file.path || "文件", file.status].filter(Boolean).join(" · "));
  $("review-diff").textContent = [files.join("\n"), material.diff || ""].filter(Boolean).join("\n\n") ||
    "没有可展示的文件差异；请按交付摘要和验证状态判断。";
  if (material.capture_error) $("review-diff").textContent += "\n\n捕获提示：" +
    (material.capture_error.message || material.capture_error.code || "请人工检查交付。");
  $("review-note").value = "";
  $("review-checked").checked = false;
  $("review-accept").disabled = true;
  $("review-reject").disabled = true;
  dialog.showModal();
  $("review-checked").focus();
}

function ownerBearer() {
  let bearer = "";
  try { bearer = sessionStorage.getItem(ownerKey) || ""; } catch { /* unavailable */ }
  if (!bearer) throw new Error("本机管理者会话不可用。请先从本机工作台进入。");
  return bearer;
}

async function refreshHumanForward() {
  const turn = generation;
  const humanBearer = token;
  const tools = $("human-forward-tools");
  tools.hidden = true;
  try {
    const status = await request("human-forward/status", "GET", undefined, ownerBearer());
    if (turn !== generation || !token || token !== humanBearer) return;
    $("human-forward-error").textContent = status.error
      ? "上次启动后未能恢复 Human 入口。检查端口占用，再重新启用；此前的 SSH 转发此时不可用。" : "";
    $("human-forward-error").hidden = !status.error;
    const command = $("human-forward-command");
    command.textContent = status.enabled
      ? `在另一台电脑运行 ssh -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127.0.0.1:49180:127.0.0.1:${status.port} user@home-host（替换 SSH 主机名），保持终端打开，再访问 http://127.0.0.1:49180/human；关闭终端即结束转发。`
      : "";
    command.hidden = !status.enabled;
    $("human-forward-toggle").textContent = status.enabled ? "停止 Human 远程入口" : "启用 SSH 转发入口";
    $("human-forward-toggle").dataset.enabled = status.enabled ? "true" : "false";
    tools.hidden = false;
  } catch { /* Invalid or remote owner credentials never reveal this control. */ }
}

async function refreshFleetListener() {
  const turn = generation;
  const humanBearer = token;
  const tools = $("fleet-listener-tools");
  tools.hidden = true;
  try {
    const status = await request("fleet/status", "GET", undefined, ownerBearer());
    if (turn !== generation || !token || token !== humanBearer) return;
    const address = $("fleet-listener-address");
    if (status.enabled && status.base_url) address.value = new URL(status.base_url).hostname;
    address.disabled = status.enabled;
    $("fleet-listener-state").textContent = status.enabled
      ? "设备监听已启用：" + status.base_url + "。项目尚需另行授权。" +
        (status.lan_advertised ? "同网发现已发布。" : status.lan_error
          ? "同网广播不可用，可使用完整邀请和手动地址。" : "当前地址不向同网广播，可使用手动地址或 SSH 转发。")
      : "设备监听未启用。";
    const sshGuide = $("fleet-ssh-guide");
    const endpoint = status.enabled && status.base_url ? new URL(status.base_url) : null;
    sshGuide.hidden = !endpoint;
    sshGuide.textContent = endpoint
      ? `不同网络且已有 SSH 登录时，可在目标设备运行 ssh -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127.0.0.1:49181:${endpoint.hostname}:${endpoint.port} USER@SSH_HOST（替换用户、SSH 主机；49181 须为空闲本机端口），保持终端打开，再把完整邀请中的 base_url 改为 https://127.0.0.1:49181。关闭终端即结束转发；保留原证书指纹与邀请码，SSH 转发本身不授予项目访问权。`
      : "";
    $("fleet-listener-toggle").textContent = status.enabled ? "停止设备监听" : "启用设备监听";
    $("fleet-listener-toggle").dataset.enabled = status.enabled ? "true" : "false";
    tools.hidden = false;
  } catch { /* Invalid or remote owner credentials never reveal this control. */ }
}

async function refreshFleetConnection() {
  const turn = generation;
  const humanBearer = token;
  const tools = $("fleet-join-tools");
  tools.hidden = true;
  try {
    const status = await request("fleet/connection", "GET", undefined, ownerBearer());
    if (turn !== generation || !token || token !== humanBearer) return;
    $("fleet-join-state").textContent = status.configured
      ? "此设备已保存主控配对：" + status.base_url + "。这里不代表主控此刻在线。"
      : status.saved_pairing
        ? "已有配对身份，但主控当前未连接；请先核对主控地址与网络。"
        : "此设备尚未加入主控。";
    const canJoin = !status.saved_pairing;
    $("fleet-join-invite").hidden = !canJoin;
    $("fleet-join-tools").querySelector("label").hidden = !canJoin;
    $("fleet-join-submit").hidden = !canJoin;
    $("fleet-relocate-form").hidden = !status.saved_pairing;
    $("fleet-leave-open").hidden = !status.saved_pairing;
    const mappingForm = $("fleet-map-form");
    const projectSelect = $("fleet-map-project");
    const previousProject = projectSelect.value;
    projectSelect.replaceChildren(...(status.projects || []).map((project) => {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = project.name;
      return option;
    }));
    if ((status.projects || []).some((project) => project.id === previousProject)) {
      projectSelect.value = previousProject;
    }
    $("fleet-map-path").value = status.mappings?.[projectSelect.value] || "";
    $("fleet-map-state").textContent = projectSelect.value && status.mappings?.[projectSelect.value]
      ? "已选目录：" + status.mappings[projectSelect.value] + "。接下来仍需加入本机员工。"
      : "尚未为此项目选择本机目录。";
    mappingForm.hidden = !status.configured || !(status.projects || []).length;
    const employeeForm = $("fleet-employee-form");
    employeeForm.hidden = true;
    tools.hidden = false;
    if (!mappingForm.hidden && status.mappings?.[projectSelect.value]) {
      let available;
      try { available = await request("fleet/available-employees", "GET", undefined, ownerBearer()); }
      catch (error) {
        if (turn !== generation || !token || token !== humanBearer) return;
        $("fleet-employee-kind").replaceChildren();
        $("fleet-employee-submit").disabled = true;
        $("fleet-employee-state").textContent = "本机工具检查失败：" + error.message;
        employeeForm.hidden = false;
        return;
      }
      if (turn !== generation || !token || token !== humanBearer) return;
      const kinds = $("fleet-employee-kind");
      kinds.replaceChildren(...(available.employees || []).map((employee) => {
        const option = document.createElement("option");
        option.value = employee.kind;
        option.textContent = employee.name;
        return option;
      }));
      $("fleet-employee-submit").disabled = !kinds.options.length;
      $("fleet-employee-state").textContent = kinds.options.length
        ? "选择本机工具并为这位员工命名。"
        : "本机尚未发现可用于受管执行的 CLI。项目邮箱仍可由其他宿主使用。";
      employeeForm.hidden = false;
    }
  } catch { /* Invalid or remote owner credentials never reveal this control. */ }
}

function confirmMemberRemoval(memberName) {
  const dialog = $("member-removal-confirm");
  $("member-removal-name").textContent = memberName;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "remove";
      $("member-removal-name").textContent = "";
      resolve(approved);
    }, { once: true });
  });
}

function confirmReviewerChange(memberName, grant) {
  const dialog = $("reviewer-confirm");
  $("reviewer-confirm-description").textContent = grant
    ? `允许 ${memberName} 阅读本项目待验收交付并作出通过或退回决定。此权限不包含成员管理或其他项目。`
    : `撤销 ${memberName} 在本项目的验收权；其已有的项目查看或编辑权限保持不变。`;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      $("reviewer-confirm-description").textContent = "";
      resolve(dialog.returnValue === "confirm");
    }, { once: true });
  });
}

function listItem(text, secondary = "") {
  const li = document.createElement("li");
  li.textContent = text;
  if (secondary) {
    const small = document.createElement("span");
    small.className = "scope-tag";
    small.textContent = secondary;
    li.append(small);
  }
  return li;
}

function renderCollections(spaces) {
  const spaceNodes = spaces.map((space) => listItem(space.name,
    space.kind === "private" ? "私人" : "共享"));
  $("spaces").replaceChildren(...(spaceNodes.length ? spaceNodes : [listItem("暂无可访问空间")]));
  $("space-count").textContent = `${spaces.length} 个`;
  const projectNodes = projects.map((project) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.projectId = project.id;
    button.textContent = project.name;
    button.setAttribute("aria-current", String(project.id === selectedProjectId));
    const small = document.createElement("small");
    small.textContent = project.kind === "private" ? "私人项目" : "共享项目";
    button.append(small);
    li.append(button);
    return li;
  });
  $("projects").replaceChildren(...(projectNodes.length ? projectNodes :
    [listItem("暂无项目。可以先创建一个私人项目。")]));
  $("project-count").textContent = `${projects.length} 个`;
}

function recordSection(title, items, renderItem, empty) {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  if (!items.length) {
    const text = document.createElement("p");
    text.className = "empty";
    text.textContent = empty;
    section.append(text);
    return section;
  }
  const list = document.createElement("ul");
  list.className = "record-list";
  for (const item of items) {
    const row = document.createElement("li");
    renderItem(row, item);
    list.append(row);
  }
  section.append(list);
  return section;
}

function deviceInviteSection(base, turn) {
  const humanBearer = token;
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = "添加设备到此项目";
  const description = document.createElement("p");
  description.textContent = "本机管理者先启用设备监听；只有项目所有者可签发一次性邀请。每台设备只获得这次邀请指定的项目权限。";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "quiet-button";
  button.textContent = "生成此项目设备邀请码";
  button.addEventListener("click", async () => {
    if (turn !== generation || token !== humanBearer) return;
    button.disabled = true;
    try {
      const invite = await request(base + "/fleet/invites", "POST", {});
      if (turn !== generation || token !== humanBearer) return;
      $("fleet-invite-code").value = JSON.stringify(invite, null, 2);
      $("fleet-invite-receipt").showModal();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });
  const listButton = document.createElement("button");
  listButton.type = "button";
  listButton.className = "quiet-button";
  listButton.textContent = "查看已配对设备";
  const list = document.createElement("ul");
  list.className = "record-list";
  list.setAttribute("aria-label", "此项目已配对设备");
  list.hidden = true;
  async function loadDevices() {
    if (turn !== generation || token !== humanBearer) return;
    const response = await request(base + "/fleet/devices");
    if (turn !== generation || token !== humanBearer) return;
    list.replaceChildren();
    for (const device of response.devices || []) {
      const row = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = device.name;
      const id = document.createElement("small");
      id.textContent = device.device_id;
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "quiet-button";
      revoke.textContent = "撤销此项目权限";
      revoke.addEventListener("click", async () => {
        if (!await confirmDeviceRevoke(device.name)) return;
        if (turn !== generation || token !== humanBearer) return;
        revoke.disabled = true;
        try {
          await request(base + "/fleet/devices/" + encodeURIComponent(device.device_id) +
            "/revoke", "POST", {});
          if (turn === generation && token === humanBearer) await loadDevices();
        } catch (error) { notice(error.message, true); }
        finally { revoke.disabled = false; }
      });
      row.append(name, id, revoke);
      list.append(row);
    }
    if (!list.children.length) list.append(listItem("此项目尚无已配对设备。"));
    list.hidden = false;
  }
  listButton.addEventListener("click", async () => {
    if (turn !== generation || token !== humanBearer) return;
    listButton.disabled = true;
    try { await loadDevices(); }
    catch (error) { notice(error.message, true); }
    finally { listButton.disabled = false; }
  });
  const actions = document.createElement("div");
  actions.className = "account-actions";
  actions.append(button, listButton);
  section.append(heading, description, actions, list);
  return section;
}

function confirmDeviceRevoke(deviceName) {
  const dialog = $("fleet-device-revoke");
  $("fleet-device-revoke-name").textContent = deviceName;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "revoke";
      $("fleet-device-revoke-name").textContent = "";
      resolve(approved);
    }, { once: true });
  });
}

function accessSection(project, base, members = [], grants = []) {
  const template = $(project.kind === "private" ? "private-share-template" : "shared-access-template");
  const section = template.content.firstElementChild.cloneNode(true);
  if (project.kind === "private") {
    const form = section.querySelector("form");
    const select = form.querySelector("select");
    const owned = spaces.filter((space) => space.kind === "shared" && space.role === "owner");
    for (const space of owned) select.add(new Option(space.name, space.id));
    if (!owned.length) {
      section.querySelector("p").textContent = "先在左侧新建共享空间，再将项目放入其中。";
      form.hidden = true;
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submit(form, async () => {
        await request(base + "/share", "POST", { space_id: select.value });
        await refresh();
        notice("项目已移入共享空间；尚未向任何成员授予访问权。");
      });
    });
    return section;
  }

  const ownSpace = spaces.find((space) => space.id === project.space_id && space.role === "owner");
  const memberForm = section.querySelector(".add-member");
  if (!ownSpace) memberForm.hidden = true;
  memberForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(memberForm, async () => {
      const target = memberForm.querySelector("input").value.trim();
      await request("human/spaces/" + encodeURIComponent(project.space_id) + "/members",
        "POST", { human_id: target });
      await showProject(project.id);
      notice("成员已加入空间；还需单独授予项目访问权。");
    });
  });

  const grantForm = section.querySelector(".grant-member");
  const [memberSelect, roleSelect] = grantForm.querySelectorAll("select");
  for (const member of members.filter((item) => item.id !== humanId)) {
    memberSelect.add(new Option(member.display_name, member.id));
  }
  if (!memberSelect.options.length) {
    grantForm.hidden = true;
    const hint = document.createElement("p");
    hint.className = "field-note";
    hint.textContent = "空间中还没有其他成员。先邀请一位 Human 加入空间。";
    grantForm.after(hint);
  }
  grantForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(grantForm, async () => {
      await request(base + "/grants", "POST", {
        human_id: memberSelect.value, role: roleSelect.value,
      });
      await showProject(project.id);
      notice("项目访问权已更新。");
    });
  });
  const list = section.querySelector(".grant-list");
  for (const grant of grants) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = grant.display_name + " · " +
      ({ owner: "所有者", editor: "编辑者", viewer: "查看者" }[grant.role] || grant.role) +
      (grant.can_review && grant.role !== "owner" ? " · 验收者" : "");
    row.append(label);
    if (grant.human_id !== humanId) {
      const reviewButton = document.createElement("button");
      reviewButton.type = "button";
      reviewButton.className = "quiet-button";
      reviewButton.textContent = grant.can_review ? "撤销验收" : "授予验收";
      reviewButton.setAttribute("aria-label", reviewButton.textContent + " · " + grant.display_name);
      reviewButton.addEventListener("click", async () => {
        reviewButton.disabled = true;
        try {
          const granting = !grant.can_review;
          if (!await confirmReviewerChange(grant.display_name, granting)) return;
          await request(base + "/reviewers" +
            (granting ? "" : "/" + encodeURIComponent(grant.human_id)),
          granting ? "POST" : "DELETE", granting ? { human_id: grant.human_id } : undefined);
          await showProject(project.id);
          notice(granting ? "已授予本项目验收权。" : "已撤销本项目验收权。");
        } catch (error) { notice(error.message, true); }
        finally { reviewButton.disabled = false; }
      });
      row.append(reviewButton);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "quiet-button";
      remove.textContent = "撤销访问";
      remove.setAttribute("aria-label", "撤销 " + grant.display_name + " 的项目访问");
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        try {
          await request(base + "/grants/" + encodeURIComponent(grant.human_id), "DELETE");
          await showProject(project.id);
          notice("已撤销该成员的项目访问权。");
        } catch (error) {
          if (error.status === 401) clearSession();
          else if (error.status === 403 || error.status === 404) suspendPrivateView();
          notice(error.message, true);
        }
        finally { remove.disabled = false; }
      });
      row.append(remove);
    }
    list.append(row);
  }
  const spaceMemberSection = section.querySelector(".space-members");
  if (!ownSpace) spaceMemberSection.hidden = true;
  const memberList = section.querySelector(".space-member-list");
  for (const member of members) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = member.display_name + (member.id === humanId ? " · 你" : "");
    row.append(label);
    if (ownSpace && member.id !== humanId) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "quiet-button";
      remove.textContent = "移出空间";
      remove.setAttribute("aria-label", "将 " + member.display_name + " 移出共享空间");
      remove.addEventListener("click", async () => {
        if (!await confirmMemberRemoval(member.display_name)) return;
        remove.disabled = true;
        try {
          await request("human/spaces/" + encodeURIComponent(project.space_id) +
            "/members/" + encodeURIComponent(member.id), "DELETE");
          await showProject(project.id);
          notice("已移出空间；该成员在此空间全部项目的后续访问权已撤销。");
        } catch (error) { notice(error.message, true); }
        finally { remove.disabled = false; }
      });
      row.append(remove);
    }
    memberList.append(row);
  }
  return section;
}

async function showProject(projectId) {
  const project = projects.find((item) => item.id === projectId);
  if (!project) return;
  selectedProjectId = projectId;
  const turn = ++generation;
  $("detail-title").textContent = project.name;
  $("detail-scope").textContent = project.kind === "private" ? "私人项目" : "共享项目";
  $("detail-scope").hidden = false;
  $("detail-description").textContent = "正在读取当前项目…";
  $("detail-body").replaceChildren();
  for (const button of $("projects").querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.projectId === projectId));
  }
  try {
    const base = "human/projects/" + encodeURIComponent(projectId);
    const [tasks, messages, manifest, members, grants] = await Promise.all([
      request(base + "/tasks"), request(base + "/messages"),
      request(base + "/resources/manifest"),
      project.role === "owner" && project.kind === "shared" ?
        request("human/spaces/" + encodeURIComponent(project.space_id) + "/members") : null,
      project.role === "owner" && project.kind === "shared" ? request(base + "/grants") : null,
    ]);
    if (turn !== generation || !token) return;
    $("detail-description").textContent = "本页仅展示项目范围内的任务摘要、群消息与已批准资料。";
    const taskSection = recordSection("任务", tasks.tasks || [], (row, item) => {
      row.textContent = item.title;
      const small = document.createElement("small"); small.textContent = item.status; row.append(small);
      if (project.can_review && item.status === "review") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "quiet-button";
        button.textContent = "查看交付并验收";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await showReview(projectId, item.id); }
          catch (error) { notice(error.message, true); }
          finally { button.disabled = false; }
        });
        row.append(button);
      }
    }, "这个项目还没有任务。");
    const messageSection = recordSection("项目群消息", messages.messages || [], (row, item) => {
      const strong = document.createElement("strong"); strong.textContent = item.title || "项目消息";
      const body = document.createElement("p"); body.textContent = item.body || "";
      row.append(strong, body);
    }, "这个项目还没有群消息。员工定向私信不会显示在这里。");
    const resourceSection = recordSection("已批准资料", manifest.resources || [], (row, item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "quiet-button";
      button.textContent = "读取资料 " + item.resource_id;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const resource = await request(base + "/resources/" +
            encodeURIComponent(item.resource_id) + "/versions/" + encodeURIComponent(item.version_id));
          if (turn !== generation || !token) return;
          const content = document.createElement("p");
          content.textContent = resource.content;
          row.querySelector("p")?.remove();
          row.append(content);
        } catch (error) { notice(error.message, true); }
        finally { button.disabled = false; }
      });
      row.append(button);
    }, "尚无已批准的固定资料版本。");
    $("detail-body").replaceChildren(taskSection, messageSection, resourceSection,
      ...(project.role === "owner" ? [deviceInviteSection(base, turn), accessSection(project, base,
        members?.members || [], grants?.grants || [])] : []));
  } catch (error) {
    if (turn !== generation) return;
    if (error.status === 401) clearSession();
    else suspendPrivateView();
    notice(error.message, true);
  }
}

async function refresh(preserveRecoveryReceipt = false) {
  if (!token) { clearSession(); return null; }
  const turn = ++generation;
  let self, spaceData, projectData;
  try {
    [self, spaceData, projectData] = await Promise.all([
      request("human/self"), request("human/spaces"), request("human/projects"),
    ]);
  } catch (error) {
    if (turn === generation) {
      if (error.status === 401) clearSession();
      else suspendPrivateView(preserveRecoveryReceipt);
    }
    throw error;
  }
  if (turn !== generation || !token) return null;
  humanId = self.human_id;
  $("identity").textContent = self.display_name;
  $("identity").hidden = false;
  $("logout").hidden = false;
  $("entry").hidden = true;
  $("workspace").hidden = false;
  $("recheck-access").hidden = true;
  let ownerAvailable = false;
  try { ownerAvailable = Boolean(sessionStorage.getItem(ownerKey)); } catch { /* unavailable */ }
  $("invite-tools").hidden = !ownerAvailable;
  if (ownerAvailable) await Promise.all([
    refreshHumanForward(), refreshFleetListener(), refreshFleetConnection(),
  ]);
  else {
    $("human-forward-tools").hidden = true;
    $("fleet-listener-tools").hidden = true;
    $("fleet-join-tools").hidden = true;
  }
  if (turn !== generation || !token) return null;
  spaces = spaceData.spaces || [];
  projects = projectData.projects || [];
  if (!projects.some((item) => item.id === selectedProjectId)) selectedProjectId = "";
  renderCollections(spaces);
  if (selectedProjectId) await showProject(selectedProjectId);
  else {
    $("detail-title").textContent = "选择一个项目";
    $("detail-scope").hidden = true;
    $("detail-description").textContent = "项目的任务、项目群消息和已批准资料会显示在这里。员工之间的定向私信不会进入此页。";
    $("detail-body").replaceChildren();
  }
  return self;
}

async function startSession(result) {
  stopAccessWatch();
  suspendPrivateView();
  token = result.token;
  try { changeProfileEpoch(true); }
  catch (error) {
    try { await request("human/logout", "POST", {}, result.token); } catch { /* clear locally even if revoke failed */ }
    clearSession();
    if (result.recovery_code) showReceipt(result.human_id, result.recovery_code);
    throw error;
  }
  try { sessionStorage.setItem(humanKey, token); } catch { /* current tab only */ }
  $("invitation").value = "";
  $("recovery-code").value = "";
  // Present the one-time code before any subsequent network request can fail.
  if (result.recovery_code) showReceipt(result.human_id, result.recovery_code);
  await refresh(Boolean(result.recovery_code));
  watchAccess();
  notice("");
}

async function submit(form, action) {
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  notice("");
  try { await action(); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
}

$("enroll-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    requireProfileStorage();
    leaveOwnerInThisTab();
    await startSession(await request("human/enroll", "POST", {
      invitation: $("invitation").value.trim(),
    }, ""));
  });
});
$("recover-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    requireProfileStorage();
    leaveOwnerInThisTab();
    await startSession(await request("human/recover", "POST", {
      human_id: $("recovery-human").value.trim(), recovery_code: $("recovery-code").value.trim(),
    }, ""));
  });
});
$("owner-entry").addEventListener("click", async () => {
  const button = $("owner-entry");
  button.disabled = true;
  try {
    requireProfileStorage();
    const ownerToken = sessionStorage.getItem(ownerKey) || "";
    await startSession(await request("human-sessions", "POST", {}, ownerToken));
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("refresh").addEventListener("click", async () => {
  try { notice(""); await refresh(); watchAccess(); }
  catch (error) { notice(error.message, true); }
});
$("recheck-access").addEventListener("click", async () => {
  stopAccessWatch();
  try { notice(""); await refresh(); watchAccess(); }
  catch (error) { notice(error.message, true); }
});
$("projects").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-project-id]");
  if (button) showProject(button.dataset.projectId);
});
$("create-project").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    const name = $("new-project-name").value.trim();
    if (!name) return;
    const created = await request("human/projects", "POST", { name });
    $("new-project-name").value = "";
    await refresh();
    await showProject(created.project_id);
    notice("私人项目已创建。");
  });
});
$("create-space").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    const name = $("new-space-name").value.trim();
    if (!name) return;
    await request("human/spaces", "POST", { name });
    $("new-space-name").value = "";
    await refresh();
    notice("共享空间已建立。项目与成员仍需分别加入。");
  });
});
$("create-human").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    const created = await request("humans", "POST", {
      display_name: $("new-human-name").value.trim(),
    }, ownerBearer());
    $("new-human-result").textContent = "新成员 Human ID：" + created.human_id;
    $("new-human-result").hidden = false;
    $("invite-human-id").value = created.human_id;
    $("new-human-name").value = "";
    notice("身份已创建。请妥善保存 Human ID，再生成邀请码。");
  });
});
$("invite-human").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    const target = $("invite-human-id").value.trim();
    const issued = await request("human-invitations", "POST", { human_id: target }, ownerBearer());
    showInvitation(issued.human_id, issued.invitation);
    notice("");
  });
});
$("human-forward-toggle").addEventListener("click", async () => {
  const button = $("human-forward-toggle");
  button.disabled = true;
  try {
    await request(button.dataset.enabled === "true" ? "human-forward/stop" : "human-forward/start",
      "POST", {}, ownerBearer());
    await refreshHumanForward();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("fleet-listener-toggle").addEventListener("click", async () => {
  const button = $("fleet-listener-toggle");
  button.disabled = true;
  try {
    const enabled = button.dataset.enabled === "true";
    await request(enabled ? "fleet/stop" : "fleet/start", "POST",
      enabled ? {} : { address: $("fleet-listener-address").value.trim() }, ownerBearer());
    await refreshFleetListener();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("fleet-discover").addEventListener("click", async () => {
  const button = $("fleet-discover");
  const results = $("fleet-discovery-results");
  button.disabled = true;
  results.textContent = "正在查找同网主控…";
  const turn = generation;
  const humanBearer = token;
  try {
    const listing = await request("fleet/discover", "GET", undefined, ownerBearer());
    if (turn !== generation || !token || token !== humanBearer) return;
    results.replaceChildren();
    if (!listing.devices.length) {
      results.textContent = "未找到同网主控。仍可使用项目所有者交给你的完整邀请手动加入。";
      return;
    }
    for (const device of listing.devices) {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.className = "quiet-button";
      choice.textContent = "使用发现的地址 " + device.base_url;
      choice.addEventListener("click", () => {
        const inviteField = $("fleet-join-invite");
        if ($("fleet-relocate-form").hidden) {
          let invite;
          try { invite = JSON.parse(inviteField.value); }
          catch { notice("先粘贴完整的配对邀请，再选择发现的地址。", true); return; }
          if (!invite || typeof invite !== "object" || Array.isArray(invite)
              || !invite.fingerprint || !invite.invite_id || !invite.invite_secret) {
            notice("配对邀请不完整。请向项目所有者索取完整邀请。", true);
            return;
          }
          invite.base_url = device.base_url;
          inviteField.value = JSON.stringify(invite, null, 2);
          notice("已填入未经核验的地址。点“验证并加入主控”时仍会核对邀请中的证书指纹。 ");
        } else {
          $("fleet-relocate-url").value = device.base_url;
          notice("已填入未经核验的地址。点“核验并更新地址”后才会保存。 ");
        }
      });
      results.append(choice);
    }
  } catch (error) {
    if (turn === generation && token === humanBearer) results.textContent = error.message;
  } finally { button.disabled = false; }
});
$("fleet-join-submit").addEventListener("click", async () => {
  const button = $("fleet-join-submit");
  let invite;
  try {
    invite = JSON.parse($("fleet-join-invite").value);
    if (!invite || typeof invite !== "object" || Array.isArray(invite)) throw new Error();
  } catch {
    notice("请粘贴完整的设备配对信息（JSON）。", true);
    return;
  }
  button.disabled = true;
  try {
    await request("fleet/join", "POST", { invite }, ownerBearer());
    $("fleet-join-invite").value = "";
    await refreshFleetConnection();
    notice("设备身份已保存。接下来仍需为项目选择本机目录与员工。");
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("fleet-relocate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("fleet-relocate-form").querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await request("fleet/relocate", "POST", {
      base_url: $("fleet-relocate-url").value.trim(),
    }, ownerBearer());
    $("fleet-relocate-url").value = "";
    await refreshFleetConnection();
    notice("已核验并保存原主控的新地址。设备身份未改变。");
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("fleet-leave-open").addEventListener("click", () => {
  $("fleet-leave-confirm").returnValue = "";
  $("fleet-leave-confirm").showModal();
});
$("fleet-leave-confirm").addEventListener("close", async () => {
  if ($("fleet-leave-confirm").returnValue !== "leave") return;
  const button = $("fleet-leave-open");
  button.disabled = true;
  try {
    await request("fleet/leave", "POST", {}, ownerBearer());
    await refreshFleetConnection();
    notice("本机配对已断开。请主控项目所有者撤销旧设备身份，再签发新邀请。");
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("fleet-map-project").addEventListener("change", () => { refreshFleetConnection(); });
$("fleet-map-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("fleet-map-form").querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await request("fleet/map", "POST", {
      project_id: $("fleet-map-project").value,
      path: $("fleet-map-path").value.trim(),
    }, ownerBearer());
    await refreshFleetConnection();
    notice("本机项目目录已保存。接下来仍需加入此设备的员工。");
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("fleet-employee-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("fleet-employee-submit");
  button.disabled = true;
  try {
    await request("fleet/employee", "POST", {
      project_id: $("fleet-map-project").value,
      name: $("fleet-employee-name").value.trim(),
      kind: $("fleet-employee-kind").value,
    }, ownerBearer());
    $("fleet-employee-name").value = "";
    $("fleet-employee-state").textContent = "员工已加入主控项目。是否能执行仍取决于本机宿主配置与实际接单验收。";
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = !$("fleet-employee-kind").options.length; }
});
$("fleet-invite-close").addEventListener("click", () => {
  $("fleet-invite-receipt").close();
  $("fleet-invite-code").value = "";
});
$("fleet-invite-receipt").addEventListener("close", () => { $("fleet-invite-code").value = ""; });
$("review-checked").addEventListener("change", () => {
  const confirmed = $("review-checked").checked;
  $("review-accept").disabled = !confirmed;
  $("review-reject").disabled = !confirmed;
});
$("review-close").addEventListener("click", () => $("review-dialog").close());
for (const [id, decision] of [["review-accept", "accept"], ["review-reject", "reject"]]) {
  $(id).addEventListener("click", async () => {
    if (!$("review-checked").checked || !$("review-dialog").open) return;
    const note = $("review-note").value.trim();
    if (decision === "reject" && !note) {
      notice("退回时请填写具体意见。", true);
      $("review-note").focus();
      return;
    }
    $("review-accept").disabled = true;
    $("review-reject").disabled = true;
    try {
      await request($("review-dialog").dataset.route, "POST", { decision, note });
      $("review-dialog").close();
      await refresh();
      notice(decision === "accept" ? "验收已通过。" : "任务已退回。");
    } catch (error) {
      notice(error.message, true);
      $("review-accept").disabled = false;
      $("review-reject").disabled = false;
    }
  });
}
$("review-dialog").addEventListener("close", () => {
  if (!$("review-dialog").open) clearReviewDialog();
});
$("logout").addEventListener("click", async () => {
  const button = $("logout");
  button.disabled = true;
  try {
    await request("human/logout", "POST", {});
    changeProfileEpoch(false);
    clearSession();
    notice("已退出当前 Human 身份。");
  } catch (error) { notice("未能撤销服务端会话，请保持此页面并重试。" + error.message, true); }
  finally { button.disabled = false; }
});
$("renew").addEventListener("click", async () => {
  stopAccessWatch();
  try {
    const renewed = await request("human/session/renew", "POST", {});
    token = renewed.token;
    try { sessionStorage.setItem(humanKey, token); } catch { /* current tab only */ }
    watchAccess();
    notice("会话已续期，旧会话令牌已失效。");
  } catch (error) { watchAccess(); notice(error.message, true); }
});
$("setup-recovery").addEventListener("click", async () => {
  try {
    const result = await request("human/recovery/setup", "POST", {});
    showReceipt(humanId, result.recovery_code);
  } catch (error) { notice(error.message, true); }
});
$("receipt-close").addEventListener("click", () => $("recovery-receipt").close());
$("recovery-receipt").addEventListener("close", () => {
  $("receipt-human").textContent = "";
  $("receipt-code").textContent = "";
});
$("invitation-close").addEventListener("click", () => $("invitation-receipt").close());
$("invitation-receipt").addEventListener("close", () => {
  $("invitation-human").textContent = "";
  $("invitation-code").textContent = "";
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && token) {
    if (leaveOldProfile()) return;
    stopAccessWatch();
    refresh().then(watchAccess).catch((error) => notice(error.message, true));
  }
});
if (token) {
  const shared = sharedProfileEpoch();
  if (shared === null && !profileEpoch) {
    try { changeProfileEpoch(true); }
    catch (error) { clearSession(); notice(error.message, true); }
  }
  if (token && !leaveOldProfile()) {
    refresh().then(watchAccess).catch((error) => notice(error.message, true));
  }
} else {
  try { requireProfileStorage(); } catch (error) { notice(error.message, true); }
}
