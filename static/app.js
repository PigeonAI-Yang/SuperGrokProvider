const $ = (selector) => document.querySelector(selector);
const PAGE_SIZE = 4;
const state = {
  accounts: [], agents: [], agentId: "zcode", groups: [], groupId: null, moveTargets: [], reusableGroups: [],
  activeId: null, config: null,
  authId: null, accountDrawerId: null, deleteTarget: null, moveId: null, poll: null, page: 0,
  integration: "zcode", drawerTrigger: "#open-details",
};

const statusNames = {
  pending: "待授权",
  authorizing: "授权中",
  ready: "可用",
  exhausted: "额度耗尽",
  cooldown: "暂时限流",
  error: "需要处理",
};

const membershipNames = { lite: "Lite", super: "Super", heavy: "Heavy", unknown: "未标注" };

function currentGroup() {
  return state.groups.find((group) => group.id === state.groupId) || null;
}

function currentAgent() {
  return state.agents.find((agent) => agent.id === state.agentId) || null;
}

function renderAgentTabs() {
  const tabs = $("#agent-tabs");
  tabs.replaceChildren();
  for (const agent of state.agents) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = agent.name;
    button.dataset.agentId = agent.id;
    button.setAttribute("aria-current", agent.id === state.agentId ? "page" : "false");
    button.title = `${agent.group_count} 个组 · ${agent.account_count} 个账号`;
    tabs.append(button);
  }
}

function fillGroupSelect(select, selectedId = state.groupId, excludedId = null) {
  const groups = state.groups.filter((group) => group.id !== excludedId);
  const signature = JSON.stringify(groups.map((group) => [group.id, group.name]));
  if (select.dataset.signature !== signature) {
    select.replaceChildren();
    for (const group of groups) {
      const option = document.createElement("option");
      option.value = group.id;
      option.textContent = group.name;
      select.append(option);
    }
    select.dataset.signature = signature;
  }
  if (selectedId && groups.some((group) => group.id === selectedId)) select.value = selectedId;
}

function fillMoveTargets(excludedId) {
  const select = $("#move-group");
  const targets = state.moveTargets.filter((group) => group.id !== excludedId);
  select.replaceChildren();
  for (const group of targets) {
    const option = document.createElement("option");
    option.value = group.id;
    option.textContent = `${group.agent_name} / ${group.name}`;
    select.append(option);
  }
}

function renderGroupControls() {
  const group = currentGroup();
  const agent = currentAgent();
  if (!group || !agent) return;
  renderAgentTabs();
  fillGroupSelect($("#group-select"));
  const accountGroup = $("#account-group");
  const pendingGroup = $("#account-dialog").open ? accountGroup.value : state.groupId;
  fillGroupSelect(accountGroup, pendingGroup || state.groupId);
  $("#provider-group-name").textContent = `${agent.name} · 连接信息`;
  $("#groups-title").textContent = `${agent.name} · 管理账号组`;
  if (document.activeElement !== $("#rename-group-name")) $("#rename-group-name").value = group.name;
  $("#group-account-count").textContent = group.account_count;
  $("#group-ready-count").textContent = group.ready_count;
  const deleteButton = $("#delete-group");
  deleteButton.hidden = group.is_last;
  deleteButton.disabled = !group.source_group_id && group.account_count > 0;
  deleteButton.textContent = group.source_group_id ? "移除当前复用组" : (group.account_count > 0 ? `先移走 ${group.account_count} 个账号` : "删除当前账号组");
  const toggle = $("#toggle-group");
  toggle.textContent = group.enabled ? "已启用 · 点击隔离" : "已隔离 · 点击启用";
  toggle.classList.toggle("isolated", !group.enabled);
  const index = state.groups.findIndex((item) => item.id === group.id);
  $("#move-group-up").disabled = index <= 0;
  $("#move-group-down").disabled = index < 0 || index >= state.groups.length - 1;
  const isMcp = agent.kind === "mcp";
  $("#add-account").hidden = isMcp;
  const nameInput = $("#new-group-name");
  const sourceSelect = $("#reuse-group-source");
  nameInput.hidden = isMcp;
  nameInput.required = !isMcp;
  sourceSelect.hidden = !isMcp;
  sourceSelect.required = isMcp;
  if (isMcp) {
    sourceSelect.replaceChildren();
    const used = new Set(state.groups.map((item) => item.source_group_id));
    for (const source of state.reusableGroups.filter((item) => !used.has(item.id))) {
      const option = document.createElement("option");
      option.value = source.id;
      option.textContent = source.name;
      sourceSelect.append(option);
    }
  }
  $("#create-group-form button").textContent = isMcp ? "复用账号组" : "新建";
}

function addedAtLabel(value) {
  if (!value) return "添加时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "添加时间未知";
  return `添加于 ${new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date)}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || `请求失败 (${response.status})`);
  return data;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = error ? "error" : "";
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3200);
}

function createButton(label, action, id, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.id = id;
  button.className = className;
  return button;
}

function usageLabel(account) {
  if (account.usage_error) {
    const cached = account.usage_percent == null ? "" : `，保留上次 ${account.usage_percent.toFixed(1)}% 结果`;
    return { text: `本次额度刷新失败${cached}`, error: true };
  }
  if (account.usage_percent == null) return { text: "额度等待检查", error: false, percent: null };
  const reset = account.usage_period_end
    ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(account.usage_period_end))
    : "未知时间";
  return { text: "", error: false, percent: account.usage_percent, reset };
}

function renderAccounts() {
  const list = $("#account-list");
  list.replaceChildren();
  $("#loading-state").hidden = true;
  $("#empty-state").hidden = state.accounts.length !== 0;
  $("#account-summary").textContent = state.accounts.length
    ? `${state.accounts.length} 个账号，${state.accounts.filter((a) => a.state === "ready" && a.enabled).length} 个可用`
    : "尚未添加账号";

  const pageCount = Math.max(1, Math.ceil(state.accounts.length / PAGE_SIZE));
  state.page = Math.min(state.page, pageCount - 1);
  const visibleAccounts = state.accounts.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  for (const account of visibleAccounts) {
    const row = document.createElement("article");
    row.className = `account-row${account.id === state.activeId ? " active" : ""}`;

    const identity = document.createElement("div");
    identity.className = "account-identity";
    identity.tabIndex = 0;
    identity.setAttribute("role", "button");
    identity.setAttribute("aria-label", `打开 ${account.name} 的账号详情`);
    identity.dataset.accountId = account.id;
    const titleLine = document.createElement("div");
    titleLine.className = "account-title-line";
    const title = document.createElement("h3");
    title.textContent = account.name;
    const badge = document.createElement("span");
    badge.className = `badge ${account.state}`;
    badge.textContent = account.enabled ? (statusNames[account.state] || account.state) : "已停用";
    titleLine.append(title, badge);
    const membership = document.createElement("span");
    membership.className = `badge membership ${account.membership_type || "unknown"}`;
    membership.textContent = membershipNames[account.membership_type] || membershipNames.unknown;
    titleLine.append(membership);
    if (account.id === state.activeId) {
      const active = document.createElement("span");
      active.className = "badge ready";
      active.textContent = "当前";
      titleLine.append(active);
    }
    identity.append(titleLine);
    const email = document.createElement("div");
    email.className = "account-email";
    email.textContent = `${account.email || "等待官方账号信息"} · ${addedAtLabel(account.created_at)}`;
    identity.append(email);
    const usage = usageLabel(account);
    const usageNode = document.createElement("div");
    usageNode.className = `account-usage${usage.error ? " error" : ""}`;
    if (usage.percent == null) {
      usageNode.textContent = usage.text;
    } else {
      const meter = document.createElement("span");
      meter.className = `usage-meter${usage.percent >= 100 ? " exhausted" : ""}`;
      meter.setAttribute("role", "progressbar");
      meter.setAttribute("aria-label", "额度已用百分比");
      meter.setAttribute("aria-valuemin", "0");
      meter.setAttribute("aria-valuemax", "100");
      meter.setAttribute("aria-valuenow", String(usage.percent));
      const fill = document.createElement("span");
      fill.style.width = `${Math.max(0, Math.min(100, usage.percent))}%`;
      meter.append(fill);
      const percent = document.createElement("strong");
      percent.textContent = `${usage.percent.toFixed(1)}%`;
      const reset = document.createElement("span");
      reset.textContent = `${usage.reset} 重置`;
      usageNode.append(meter, percent, reset);
    }
    identity.append(usageNode);
    if (account.last_error && !(account.state === "exhausted" && account.last_error === "额度已用完")) {
      const error = document.createElement("p");
      error.className = "account-error";
      error.textContent = account.last_error;
      identity.append(error);
    }

    const actions = document.createElement("div");
    actions.className = "account-actions";
    if (!account.shared && ["pending", "error"].includes(account.state)) actions.append(createButton("重新授权", "authorize", account.id));
    if (!account.shared && ["exhausted", "cooldown", "error"].includes(account.state)) actions.append(createButton("重置状态", "reset", account.id));
    if (!account.shared && account.state === "ready" && account.id !== state.activeId && account.enabled) actions.append(createButton("设为当前", "select", account.id));
    if (!account.shared && account.state === "ready") actions.append(createButton(account.enabled ? "停用" : "启用", "toggle", account.id));
    if (!["pending", "authorizing"].includes(account.state)) actions.append(createButton("刷新额度", "usage", account.id));
    if (!account.shared && state.moveTargets.length > 1) actions.append(createButton("移动", "move", account.id));
    if (!account.shared) actions.append(createButton("删除", "delete", account.id, "danger"));
    row.append(identity, actions);
    list.append(row);
  }
  const pagination = $("#pagination");
  pagination.hidden = state.accounts.length <= PAGE_SIZE;
  $("#page-label").textContent = `第 ${state.page + 1} / ${pageCount} 页`;
  $("#page-prev").disabled = state.page === 0;
  $("#page-next").disabled = state.page >= pageCount - 1;
}

function renderConfig() {
  if (!state.config) return;
  renderGroupControls();
  $("#provider-url").value = state.config.provider_url;
  $("#provider-key").value = state.config.api_key;
  $("#client-example").textContent = `OPENAI_BASE_URL=${state.config.provider_url}\nOPENAI_API_KEY=sgr_••••••••••••`;
  const proxyText = state.config.system_proxy.enabled ? `已开启 ${state.config.system_proxy.server}` : "已关闭";
  $("#proxy-status").textContent = proxyText;
  $("#drawer-proxy").textContent = proxyText;
  $("#upstream-value").textContent = state.config.upstream;
  renderIntegration();
}

function renderIntegration() {
  const integration = state.config?.integrations?.[state.integration];
  if (!integration) return;
  $("#integration-label").textContent = integration.label;
  $("#integration-filename").textContent = integration.filename;
  $("#integration-note").textContent = integration.note;
  $("#integration-config").textContent = integration.content;
  document.querySelectorAll("[data-integration]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.integration === state.integration));
  });
}

function updateAuthDialog() {
  if (!state.authId) return;
  const account = state.accounts.find((item) => item.id === state.authId);
  if (!account) return;
  $("#device-code").textContent = account.device_code || "正在生成";
  $("#auth-progress").textContent = account.auth_output || account.last_error || "等待官方授权";
  const link = $("#auth-link");
  if (account.auth_url) link.href = account.auth_url;
  else link.removeAttribute("href");
  if (account.state === "ready") {
    stopPolling();
    $("#account-dialog").close();
    toast(`${account.name} 已授权`);
  } else if (account.state === "error") {
    stopPolling();
    toast(account.last_error || "授权失败", true);
  }
}

async function load() {
  try {
    const agentQuery = `agent_id=${encodeURIComponent(state.agentId)}`;
    const groupQuery = state.groupId ? `&group_id=${encodeURIComponent(state.groupId)}` : "";
    const [accounts, config] = await Promise.all([
      api(`/api/accounts?${agentQuery}${groupQuery}`),
      api(`/api/config?${agentQuery}`),
    ]);
    state.accounts = accounts.accounts;
    state.agents = accounts.agents;
    state.groups = accounts.groups;
    state.moveTargets = accounts.move_targets;
    state.reusableGroups = accounts.reusable_groups || [];
    state.agentId = accounts.selected_agent_id;
    state.groupId = accounts.selected_group_id;
    state.activeId = accounts.active_id;
    state.config = config;
    renderAccounts();
    renderConfig();
    updateAuthDialog();
    renderBudgetAlert();
  } catch (error) {
    $("#loading-state").hidden = true;
    toast(error.message, true);
    throw error;
  }
}

function startPolling() {
  stopPolling();
  state.poll = setInterval(() => load().catch(() => {}), 1300);
}
function stopPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = null;
}

function openCreateDialog() {
  state.authId = null;
  $("#create-step").hidden = false;
  $("#auth-step").hidden = true;
  $("#account-form").reset();
  fillGroupSelect($("#account-group"));
  $("#account-dialog").showModal();
  $("#account-name").focus();
}

function openDeleteDialog(id) {
  const account = state.accounts.find((item) => item.id === id);
  if (!account) return toast("账号已不存在，请刷新后重试", true);
  state.deleteTarget = { type: "account", id };
  $("#delete-kicker").textContent = "删除账号";
  $("#delete-target-name").textContent = account.name;
  $("#delete-description").textContent = "该账号的本机授权文件会一并清除，此操作无法撤销。";
  $("#delete-error").hidden = true;
  $("#delete-error").textContent = "";
  $("#delete-dialog").showModal();
  $("#cancel-delete").focus();
}

function openGroupDeleteDialog() {
  const group = currentGroup();
  if (!group || group.is_last || group.account_count) return;
  state.deleteTarget = { type: "group", id: group.id };
  $("#delete-kicker").textContent = "删除分组";
  $("#delete-target-name").textContent = group.name;
  $("#delete-description").textContent = "该账号组会从当前 Agent 的兜底队列中移除，此操作无法撤销。";
  $("#delete-error").hidden = true;
  $("#delete-error").textContent = "";
  $("#delete-dialog").showModal();
  $("#cancel-delete").focus();
}

function openMoveDialog(id) {
  const account = state.accounts.find((item) => item.id === id);
  if (!account) return toast("账号已不存在，请刷新后重试", true);
  state.moveId = id;
  $("#move-account-name").textContent = account.name;
  fillMoveTargets(account.group_id);
  $("#move-error").hidden = true;
  $("#move-error").textContent = "";
  $("#move-dialog").showModal();
  $("#move-group").focus();
}

function openAccountDrawer(id) {
  const account = state.accounts.find((item) => item.id === id);
  if (!account) return toast("账号已不存在，请刷新后重试", true);
  state.accountDrawerId = id;
  state.drawerTrigger = `[data-account-id="${id}"]`;
  hideDrawers();
  $("#drawer-backdrop").hidden = false;
  $("#account-drawer").hidden = false;
  $("#account-drawer-title").textContent = account.name;
  $("#rename-account-name").value = account.name;
  $("#account-drawer-email").textContent = account.email || "等待官方账号信息";
  $("#account-drawer-membership").textContent = membershipNames[account.membership_type] || membershipNames.unknown;
  const group = state.moveTargets.find((item) => item.id === account.group_id);
  $("#account-drawer-location").textContent = group ? `${group.agent_name} / ${group.name}` : "未知";
  $("#account-drawer-created").textContent = addedAtLabel(account.created_at).replace("添加于 ", "");
  const policy = account.budget_policy || {};
  $("#budget-enabled").checked = policy.enabled !== false;
  $("#budget-hours").value = policy.window_hours ?? 5;
  $("#budget-percent").value = policy.limit_percent ?? 5;
  $("#budget-policy-state").textContent = policy.enabled === false
    ? "闸门已关闭；勾选并保存可重新启用"
    : policy.permanent_override
      ? "已永久解除；保存闸门可重新启用"
      : policy.override_until
      ? `本周期已解除至 ${new Date(policy.override_until).toLocaleString("zh-CN")}`
      : "默认启用；达到上限后 MCP 自动切换下一个账号";
  $("#rename-account-error").hidden = true;
  $("#rename-account-name").focus();
  loadAccountModels(id);
}

function renderBudgetAlert() {
  const alert = state.agents.find((item) => item.id === "codex-mcp")?.budget_alert;
  const dialog = $("#budget-alert-dialog");
  if (!alert || alert.acknowledged) {
    if (dialog.open) dialog.close();
    return;
  }
  if (dialog.open) return;
  const select = $("#budget-alert-account");
  select.replaceChildren();
  for (const account of alert.accounts || []) {
    const option = document.createElement("option");
    option.value = account.id;
    option.textContent = account.name;
    select.append(option);
  }
  if (select.options.length) dialog.showModal();
}

async function loadAccountModels(id) {
  const stateNode = $("#account-model-state");
  const list = $("#account-model-list");
  stateNode.textContent = "正在查询";
  stateNode.className = "";
  list.replaceChildren();
  try {
    const result = await api(`/api/accounts/${id}/models`);
    if (state.accountDrawerId !== id) return;
    const source = result.source === "account" ? "当前账号" : "Agent 参考";
    stateNode.textContent = result.models.length ? `${result.models.length} 个 · ${source}` : "暂无模型";
    for (const model of result.models) {
      const item = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = model.id;
      const reasoning = document.createElement("span");
      reasoning.textContent = model.reasoning;
      item.append(name, reasoning);
      list.append(item);
    }
  } catch (error) {
    if (state.accountDrawerId !== id) return;
    stateNode.textContent = "查询失败";
    stateNode.className = "error";
    list.textContent = error.message;
  }
}

async function mutate(path, method = "POST", body = {}) {
  const options = { method };
  if (method !== "DELETE") options.body = JSON.stringify(body);
  await api(path, options);
  await load();
}

$("#add-account").addEventListener("click", openCreateDialog);
$("#agent-tabs").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-agent-id]");
  if (!button || button.dataset.agentId === state.agentId) return;
  state.agentId = button.dataset.agentId;
  state.groupId = null;
  state.page = 0;
  const agent = state.agents.find((item) => item.id === state.agentId);
  if (agent && ["zcode", "grok_build", "hermes"].includes(agent.kind)) state.integration = agent.kind;
  await load();
});
$("#group-select").addEventListener("change", async (event) => {
  state.groupId = event.target.value;
  state.page = 0;
  await load();
});
$("#page-prev").addEventListener("click", () => { state.page -= 1; renderAccounts(); });
$("#page-next").addEventListener("click", () => { state.page += 1; renderAccounts(); });
$("#account-list").addEventListener("click", (event) => {
  const identity = event.target.closest("[data-account-id]");
  if (identity) openAccountDrawer(identity.dataset.accountId);
});
$("#account-list").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const identity = event.target.closest("[data-account-id]");
  if (!identity) return;
  event.preventDefault();
  openAccountDrawer(identity.dataset.accountId);
});
$("#open-details").addEventListener("click", () => {
  state.drawerTrigger = "#open-details";
  $("#drawer-backdrop").hidden = false;
  $("#details-drawer").hidden = false;
  $("#close-details").focus();
});
$("#manage-groups").addEventListener("click", () => {
  state.drawerTrigger = "#manage-groups";
  $("#drawer-backdrop").hidden = false;
  $("#groups-drawer").hidden = false;
  renderGroupControls();
  $("#close-groups").focus();
});
function hideDrawers() {
  $("#drawer-backdrop").hidden = true;
  $("#details-drawer").hidden = true;
  $("#integration-drawer").hidden = true;
  $("#groups-drawer").hidden = true;
  $("#account-drawer").hidden = true;
}
function closeDetails() {
  hideDrawers();
  $(state.drawerTrigger)?.focus();
}
$("#close-details").addEventListener("click", closeDetails);
$("#close-groups").addEventListener("click", closeDetails);
$("#close-account-drawer").addEventListener("click", closeDetails);
$("#drawer-backdrop").addEventListener("click", closeDetails);
$("#open-integrations").addEventListener("click", () => {
  $("#details-drawer").hidden = true;
  $("#integration-drawer").hidden = false;
  renderIntegration();
  $("#close-integrations").focus();
});
$("#close-integrations").addEventListener("click", closeDetails);
$("#back-details").addEventListener("click", () => {
  $("#integration-drawer").hidden = true;
  $("#details-drawer").hidden = false;
  $("#open-integrations").focus();
});
document.querySelectorAll("[data-integration]").forEach((button) => {
  button.addEventListener("click", () => {
    state.integration = button.dataset.integration;
    renderIntegration();
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && (
    !$("#details-drawer").hidden || !$("#integration-drawer").hidden || !$("#groups-drawer").hidden || !$("#account-drawer").hidden
  )) closeDetails();
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.action === "add") return openCreateDialog();
  if (button.classList.contains("dialog-close")) {
    stopPolling();
    return $("#account-dialog").close();
  }
  if (button.dataset.copy) {
    const target = document.getElementById(button.dataset.copy);
    const text = button.dataset.copy === "client-example"
      ? `OPENAI_BASE_URL=${state.config.provider_url}\nOPENAI_API_KEY=${state.config.api_key}`
      : (target.value || target.textContent);
    await navigator.clipboard.writeText(text);
    return toast("已复制到剪贴板");
  }
  const { action, id } = button.dataset;
  if (!action || !id) return;
  if (action === "delete") return openDeleteDialog(id);
  if (action === "move") return openMoveDialog(id);
  button.disabled = true;
  try {
    if (action === "authorize") {
      await mutate(`/api/accounts/${id}/authorize`);
      state.authId = id;
      $("#create-step").hidden = true;
      $("#auth-step").hidden = false;
      $("#account-dialog").showModal();
      startPolling();
      return;
    }
    await mutate(`/api/accounts/${id}/${action}`);
    toast("状态已更新");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#cancel-delete").addEventListener("click", () => {
  state.deleteTarget = null;
  $("#delete-dialog").close();
});
$("#confirm-delete").addEventListener("click", async () => {
  if (!state.deleteTarget) return;
  const button = $("#confirm-delete");
  const errorNode = $("#delete-error");
  button.disabled = true;
  button.textContent = "删除中...";
  errorNode.hidden = true;
  try {
    if (state.deleteTarget.type === "group") {
      await api(`/api/groups/${state.deleteTarget.id}`, { method: "DELETE" });
      state.groupId = null;
      await load();
      closeDetails();
      toast("分组已删除");
    } else {
      await mutate(`/api/accounts/${state.deleteTarget.id}`, "DELETE");
      toast("账号已删除");
    }
    state.deleteTarget = null;
    $("#delete-dialog").close();
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "确认删除";
  }
});

$("#cancel-move").addEventListener("click", () => {
  state.moveId = null;
  $("#move-dialog").close();
});
$("#confirm-move").addEventListener("click", async () => {
  if (!state.moveId) return;
  const button = $("#confirm-move");
  const errorNode = $("#move-error");
  button.disabled = true;
  errorNode.hidden = true;
  try {
    await mutate(`/api/accounts/${state.moveId}/move`, "POST", { group_id: $("#move-group").value });
    state.moveId = null;
    $("#move-dialog").close();
    toast("账号已移动");
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("#create-group-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const isMcp = currentAgent()?.kind === "mcp";
    const group = await api("/api/groups", {
      method: "POST",
      body: JSON.stringify(isMcp
        ? { source_group_id: $("#reuse-group-source").value }
        : { name: $("#new-group-name").value, agent_id: state.agentId }),
    });
    state.groupId = group.id;
    state.page = 0;
    $("#new-group-name").value = "";
    await load();
    toast(isMcp ? "ZCODE 账号组已复用" : "分组已创建");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#rename-group-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await mutate(`/api/groups/${state.groupId}/rename`, "POST", { name: $("#rename-group-name").value });
    toast("分组已重命名");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("#delete-group").addEventListener("click", openGroupDeleteDialog);

$("#rename-account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.accountDrawerId) return;
  const button = event.submitter;
  const errorNode = $("#rename-account-error");
  button.disabled = true;
  errorNode.hidden = true;
  try {
    await mutate(`/api/accounts/${state.accountDrawerId}/rename`, "POST", { name: $("#rename-account-name").value });
    const account = state.accounts.find((item) => item.id === state.accountDrawerId);
    $("#account-drawer-title").textContent = account?.name || "账号详情";
    toast("账号名称已更新");
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("#budget-policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.accountDrawerId) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    await mutate(`/api/accounts/${state.accountDrawerId}/budget`, "POST", {
      enabled: $("#budget-enabled").checked,
      window_hours: Number($("#budget-hours").value),
      limit_percent: Number($("#budget-percent").value),
    });
    openAccountDrawer(state.accountDrawerId);
    toast("账号闸门已保存");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

async function releaseBudget(mode, accountId = state.accountDrawerId) {
  if (!accountId) return;
  await mutate(`/api/accounts/${accountId}/budget/authorize`, "POST", { mode });
  $("#budget-alert-dialog").close();
  if (state.accountDrawerId === accountId) openAccountDrawer(accountId);
  toast(mode === "keep" ? "MCP 保持暂停" : mode === "window" ? "该账号本周期已解除" : "该账号闸门已永久解除");
}
$("#budget-release-window").addEventListener("click", () => releaseBudget("window").catch((error) => toast(error.message, true)));
$("#budget-release-permanent").addEventListener("click", () => releaseBudget("permanent").catch((error) => toast(error.message, true)));
$("#budget-alert-window").addEventListener("click", () => releaseBudget("window", $("#budget-alert-account").value).catch((error) => toast(error.message, true)));
$("#budget-alert-permanent").addEventListener("click", () => releaseBudget("permanent", $("#budget-alert-account").value).catch((error) => toast(error.message, true)));
$("#budget-alert-keep").addEventListener("click", () => releaseBudget("keep", $("#budget-alert-account").value).catch((error) => toast(error.message, true)));
$("#toggle-group").addEventListener("click", async () => {
  try {
    await mutate(`/api/groups/${state.groupId}/toggle`);
    toast(currentGroup()?.enabled ? "账号组已启用" : "账号组已隔离");
  } catch (error) {
    toast(error.message, true);
  }
});
async function reorderGroup(direction) {
  try {
    await mutate(`/api/groups/${state.groupId}/reorder`, "POST", { direction });
    toast("兜底顺序已更新");
  } catch (error) {
    toast(error.message, true);
  }
}
$("#move-group-up").addEventListener("click", () => reorderGroup("up"));
$("#move-group-down").addEventListener("click", () => reorderGroup("down"));

$("#account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  try {
    const account = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        name: $("#account-name").value,
        membership_type: $("#membership-type").value,
        group_id: $("#account-group").value,
      }),
    });
    state.authId = account.id;
    $("#create-step").hidden = true;
    $("#auth-step").hidden = false;
    startPolling();
    await load();
  } catch (error) {
    toast(error.message, true);
  } finally {
    submit.disabled = false;
  }
});

$("#toggle-key").addEventListener("click", () => {
  const input = $("#provider-key");
  const visible = input.type === "text";
  input.type = visible ? "password" : "text";
  $("#toggle-key").textContent = visible ? "显示" : "隐藏";
});

$("#account-dialog").addEventListener("close", stopPolling);
load().catch(() => {});
setInterval(() => load().catch(() => {}), 2000);
