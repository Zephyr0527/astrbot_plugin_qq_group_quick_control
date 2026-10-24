const page = window.AstrBotPluginPage;

const ui = {
  grid: document.getElementById("group-grid"),
  template: document.getElementById("group-template"),
  empty: document.getElementById("empty-state"),
  noResults: document.getElementById("no-results"),
  search: document.getElementById("search-input"),
  resultCount: document.getElementById("result-count"),
  refresh: document.getElementById("refresh-button"),
  openAdd: document.getElementById("open-add-button"),
  emptyAdd: document.getElementById("empty-add-button"),
  statTotal: document.getElementById("stat-total"),
  statConfigured: document.getElementById("stat-configured"),
  globalAuto: document.getElementById("global-auto"),
  adapterState: document.getElementById("adapter-state"),
  warningPanel: document.getElementById("warning-panel"),
  warningList: document.getElementById("warning-list"),
  dialog: document.getElementById("add-dialog"),
  addForm: document.getElementById("add-form"),
  addGroupId: document.getElementById("add-group-id"),
  addRemark: document.getElementById("add-remark"),
  addError: document.getElementById("add-error"),
  closeAdd: document.getElementById("close-add-button"),
  cancelAdd: document.getElementById("cancel-add-button"),
  submitAdd: document.getElementById("submit-add-button"),
  toast: document.getElementById("toast"),
};

const state = {
  groups: [],
  global: {},
  adapters: {},
  defaults: {},
  warnings: [],
  canWrite: true,
  loading: false,
};

let toastTimer = null;

const SOURCE_LABELS = {
  onebot: "OneBot",
  observed: "历史消息",
  config: "插件配置",
  active_reply_whitelist: "主动回复白名单",
};

function applyTheme(context) {
  const dark = Boolean(context && context.isDark);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function showToast(message, kind = "info") {
  if (!ui.toast) return;
  const text = String(message || "").trim();
  if (!text) return;
  if (toastTimer) {
    window.clearTimeout(toastTimer);
  }
  ui.toast.textContent = text;
  ui.toast.dataset.kind = kind;
  ui.toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    ui.toast.hidden = true;
  }, 4600);
}

function setRowStatus(card, message, kind = "") {
  const node = card.querySelector(".row-status");
  if (!node) return;
  node.textContent = message || "";
  node.classList.toggle("ok", kind === "ok");
  node.classList.toggle("error", kind === "error");
}

function formatPercent(value) {
  const rounded = Math.round(Number(value) * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function makeChip(text, kind) {
  const chip = document.createElement("span");
  chip.className = `chip ${kind || ""}`.trim();
  chip.textContent = text;
  return chip;
}

async function requestApi(method, endpoint, payload) {
  if (!page) {
    throw new Error("未找到 AstrBot 插件页面桥接脚本，请从 AstrBot 插件详情页打开本页面。");
  }
  try {
    if (method === "GET") {
      return await page.apiGet(endpoint, payload || {});
    }
    return await page.apiPost(endpoint, payload || {});
  } catch (error) {
    const responseMessage =
      error && error.response && error.response.data
        ? error.response.data.message
        : "";
    throw new Error(responseMessage || (error && error.message) || "请求失败");
  }
}

async function loadGroups({ refresh = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  ui.refresh.disabled = true;
  ui.grid.setAttribute("aria-busy", "true");
  ui.resultCount.textContent = "正在读取群列表…";

  try {
    const params = refresh ? { refresh: "1" } : {};
    const data = await requestApi("GET", "groups", params);
    state.groups = Array.isArray(data.groups) ? data.groups : [];
    state.global = data.global || {};
    state.adapters = data.adapters || {};
    state.defaults = data.defaults || {};
    state.warnings = Array.isArray(data.warnings) ? data.warnings : [];
    state.canWrite = data.can_write !== false;
    renderAll();
  } catch (error) {
    state.groups = [];
    state.warnings = [];
    renderWarnings();
    renderStats();
    ui.grid.replaceChildren();
    ui.empty.hidden = false;
    setEmptyState("群列表读取失败", error.message);
    ui.noResults.hidden = true;
    ui.resultCount.textContent = "读取失败";
    showToast(error.message, "error");
  } finally {
    state.loading = false;
    ui.refresh.disabled = false;
    ui.openAdd.disabled = !state.canWrite;
    ui.emptyAdd.disabled = !state.canWrite;
    ui.grid.removeAttribute("aria-busy");
  }
}

function renderAll() {
  renderWarnings();
  renderStats();
  renderGroups();
}

function renderWarnings() {
  const warnings = [...state.warnings];
  if (!state.canWrite) {
    warnings.unshift("当前身份为只读（API Key），不能修改群配置。请使用 WebUI 登录管理员账号。");
  }
  if (!warnings.length) {
    ui.warningPanel.hidden = true;
    ui.warningList.replaceChildren();
    return;
  }
  ui.warningList.replaceChildren();
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.textContent = String(warning);
    ui.warningList.appendChild(item);
  }
  ui.warningPanel.hidden = false;
}

function renderStats() {
  ui.statTotal.textContent = String(state.groups.length);
  ui.statConfigured.textContent = String(
    state.groups.filter((group) => group.configured).length,
  );
  if (state.global.active_reply_enabled) {
    ui.globalAuto.textContent = "已开启";
    ui.globalAuto.title = `全局概率 ${formatPercent(
      Number(state.global.possibility_reply || 0) * 100,
    )}%`;
  } else {
    ui.globalAuto.textContent = "已关闭";
    ui.globalAuto.title = "AstrBot 全局主动回复总开关关闭";
  }

  const adapters = state.adapters || {};
  const meme = adapters.meme_manager || {};
  const stealer = adapters.stealer || {};
  const names = [];
  if (meme.detected) names.push("meme_manager");
  if (stealer.detected) names.push("stealer");
  ui.adapterState.textContent = names.length ? names.join(" + ") : "未检测到";
  ui.adapterState.title = [
    meme.detected
      ? `meme_manager ${meme.version || ""}（按 ${meme.tested_version || "测试版本"} 字段适配）`
      : "未检测到 meme_manager",
    stealer.detected
      ? `stealer ${stealer.version || ""}${stealer.gate_active ? "（已接管发送开关）" : ""}`
      : "未检测到 stealer",
  ].join("；");
}

function renderGroups() {
  ui.grid.replaceChildren();
  ui.empty.hidden = state.groups.length !== 0;

  if (!state.groups.length) {
    setEmptyState(
      "还没有可管理的群",
      "如果 OneBot 已连接，点击「刷新群列表」即可读取群列表；也可以直接输入群号添加。",
    );
    ui.noResults.hidden = true;
    ui.resultCount.textContent = "共 0 个群";
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const group of state.groups) {
    const card = ui.template.content.firstElementChild.cloneNode(true);
    fillCard(card, group);
    fragment.appendChild(card);
  }
  ui.grid.appendChild(fragment);
  applyFilter();
}

function setEmptyState(title, message) {
  const heading = ui.empty.querySelector("h2");
  const paragraph = ui.empty.querySelector("p");
  if (heading) heading.textContent = title;
  if (paragraph) paragraph.textContent = message;
}

function fillCard(card, group) {
  const groupId = String(group.group_id || "");
  card.dataset.groupId = groupId;
  card.dataset.configured = group.configured ? "true" : "false";
  card.dataset.search = [
    groupId,
    group.group_name || "",
    group.remark || "",
  ]
    .join(" ")
    .toLowerCase();

  const idNode = card.querySelector(".group-id");
  idNode.textContent = `群 ${groupId}`;

  const configuredBadge = card.querySelector(".configured-badge");
  configuredBadge.hidden = false;
  configuredBadge.textContent = group.configured ? "已单独配置" : "未单独配置";
  configuredBadge.classList.toggle("unconfigured", !group.configured);

  const sourceBadge = card.querySelector(".source-badge");
  const sourceLabel = SOURCE_LABELS[group.source] || group.source || "";
  sourceBadge.hidden = !sourceLabel;
  sourceBadge.textContent = sourceLabel;
  sourceBadge.title = "群列表来源";

  const nameNode = card.querySelector(".group-name");
  const muted = document.createElement("span");
  muted.className = "muted";
  muted.textContent = "真实群名：";
  nameNode.replaceChildren(muted);
  nameNode.appendChild(
    document.createTextNode(
      group.group_name || "未获取（只读，不会修改平台群名）",
    ),
  );
  if (Number(group.member_count) > 0) {
    const count = document.createElement("span");
    count.className = "muted";
    count.textContent = ` · ${Number(group.member_count)} 人`;
    nameNode.appendChild(count);
  }

  card.querySelector('[data-field="remark"]').value = group.remark || "";
  card.querySelector('[data-field="auto_reply_enabled"]').checked = Boolean(
    group.auto_reply_enabled,
  );
  card.querySelector('[data-field="meme_enabled"]').checked = Boolean(
    group.meme_enabled,
  );
  card.querySelector('[data-field="respond_enabled"]').checked = Boolean(
    group.respond_enabled,
  );

  const percent = Math.min(
    100,
    Math.max(0, Number(group.probability_percent ?? Number(group.reply_probability) * 100) || 0),
  );
  card.querySelector('[data-field="probability-range"]').value = String(
    Math.round(percent),
  );
  card.querySelector('[data-field="probability-number"]').value = String(
    Math.round(percent * 10) / 10,
  );
  card.querySelector('[data-field="probability-label"]').textContent =
    formatPercent(percent);

  const chips = card.querySelector(".effective-chips");
  chips.replaceChildren();
  const effective = group.effective || {};
  chips.appendChild(
    makeChip(
      effective.auto_reply ? "主动回复生效" : "主动回复未生效",
      effective.auto_reply ? "on" : "off",
    ),
  );
  chips.appendChild(
    makeChip(
      effective.meme_suppressed ? "表情包已限制" : "表情包不限制",
      effective.meme_suppressed ? "off" : "on",
    ),
  );
  chips.appendChild(
    makeChip(
      effective.respond_suppressed ? "回应已关闭" : "回应正常",
      effective.respond_suppressed ? "off" : "on",
    ),
  );

  const inheritedNote = card.querySelector(".inherited-note");
  if (group.configured) {
    inheritedNote.hidden = true;
    inheritedNote.textContent = "";
  } else {
    inheritedNote.hidden = false;
    inheritedNote.textContent =
      "未单独配置：当前显示继承值，保存后才会应用本插件开关；表情包在未配置时保持原样。";
  }

  const resetButton = card.querySelector('[data-action="reset"]');
  resetButton.hidden = !group.configured;
  resetButton.disabled = !state.canWrite;
  const saveButton = card.querySelector('[data-action="save"]');
  saveButton.disabled = !state.canWrite;

  const status = card.querySelector(".row-status");
  status.className = "row-status";
  if (group.configured) {
    status.textContent = `上次保存：${formatTime(group.updated_at)}`;
  } else {
    status.textContent = "继承全局行为";
  }
  card.dataset.saving = "false";
}

function applyFilter() {
  const query = ui.search.value.trim().toLowerCase();
  const cards = Array.from(ui.grid.querySelectorAll(".group-card"));
  let visible = 0;
  for (const card of cards) {
    const matched = !query || (card.dataset.search || "").includes(query);
    card.hidden = !matched;
    if (matched) visible += 1;
  }
  ui.noResults.hidden = visible !== 0 || cards.length === 0;
  if (query) {
    ui.resultCount.textContent = `显示 ${visible} / 共 ${cards.length} 个群`;
  } else {
    ui.resultCount.textContent = `共 ${cards.length} 个群`;
  }
}

function upsertGroup(group) {
  const index = state.groups.findIndex(
    (item) => item.group_id === group.group_id,
  );
  if (index >= 0) {
    state.groups[index] = group;
  } else {
    state.groups.push(group);
  }
}

function syncProbability(target) {
  const card = target.closest(".group-card");
  if (!card) return;
  const range = card.querySelector('[data-field="probability-range"]');
  const number = card.querySelector('[data-field="probability-number"]');
  const label = card.querySelector('[data-field="probability-label"]');
  const raw = String(target.value || "").trim();
  if (!raw) return;
  let value = Number(raw);
  if (!Number.isFinite(value)) return;
  value = Math.min(100, Math.max(0, value));

  if (target === number) {
    range.value = String(Math.round(value));
    label.textContent = formatPercent(value);
    if (Number(raw) !== value) {
      number.value = String(Math.round(value * 10) / 10);
    }
  } else {
    number.value = String(Math.round(value * 10) / 10);
    label.textContent = formatPercent(value);
  }
}

function setCardBusy(card, busy) {
  card.dataset.saving = busy ? "true" : "false";
  const save = card.querySelector('[data-action="save"]');
  const reset = card.querySelector('[data-action="reset"]');
  if (save) save.disabled = busy || !state.canWrite;
  if (reset) reset.disabled = busy || !state.canWrite;
  card.setAttribute("aria-busy", busy ? "true" : "false");
}

async function saveGroup(card) {
  if (!state.canWrite) return;
  const groupId = card.dataset.groupId;
  const remark = card.querySelector('[data-field="remark"]').value.trim();
  const percent = Number(
    card.querySelector('[data-field="probability-number"]').value,
  );
  if (!Number.isFinite(percent) || percent < 0 || percent > 100) {
    setRowStatus(card, "概率必须在 0%～100% 之间", "error");
    return;
  }

  const body = {
    group_id: groupId,
    remark,
    auto_reply_enabled: card.querySelector(
      '[data-field="auto_reply_enabled"]',
    ).checked,
    meme_enabled: card.querySelector('[data-field="meme_enabled"]').checked,
    respond_enabled: card.querySelector(
      '[data-field="respond_enabled"]',
    ).checked,
    reply_probability: Math.round(percent * 100) / 10000,
  };

  setCardBusy(card, true);
  setRowStatus(card, "正在保存…");
  try {
    const data = await requestApi("POST", "groups/save", body);
    if (!data || !data.group) {
      throw new Error("服务器未返回保存结果");
    }
    upsertGroup(data.group);
    fillCard(card, data.group);
    applyFilter();
    renderStats();
    setRowStatus(card, data.message || "已保存", "ok");
    showToast(data.message || "已保存", "success");
  } catch (error) {
    setRowStatus(card, error.message, "error");
    showToast(error.message, "error");
  } finally {
    setCardBusy(card, false);
  }
}

async function resetGroup(card) {
  if (!state.canWrite) return;
  const groupId = card.dataset.groupId;
  if (
    !window.confirm(
      `恢复群 ${groupId} 的默认配置？\n\n将关闭主动回复和表情包、保留回应，概率恢复为 20%，群备注保留。`,
    )
  ) {
    return;
  }
  setCardBusy(card, true);
  setRowStatus(card, "正在恢复…");
  try {
    const data = await requestApi("POST", "groups/reset", {
      group_id: groupId,
    });
    if (!data || !data.group) {
      throw new Error("服务器未返回恢复结果");
    }
    upsertGroup(data.group);
    fillCard(card, data.group);
    applyFilter();
    renderStats();
    setRowStatus(card, data.message || "已恢复默认", "ok");
    showToast(data.message || "已恢复默认", "success");
  } catch (error) {
    setRowStatus(card, error.message, "error");
    showToast(error.message, "error");
  } finally {
    setCardBusy(card, false);
  }
}

function openAddDialog() {
  ui.addError.hidden = true;
  ui.addError.textContent = "";
  ui.addForm.reset();
  ui.dialog.showModal();
  window.setTimeout(() => ui.addGroupId.focus(), 30);
}

function closeAddDialog() {
  ui.dialog.close();
}

function showAddError(message) {
  ui.addError.textContent = message;
  ui.addError.hidden = false;
}

async function submitAdd(event) {
  event.preventDefault();
  const groupId = ui.addGroupId.value.trim();
  const remark = ui.addRemark.value.trim();
  if (!/^\d{5,20}$/.test(groupId)) {
    showAddError("群号必须是 5-20 位数字。");
    return;
  }
  if (remark.length > 64) {
    showAddError("群备注不能超过 64 个字符。");
    return;
  }
  ui.submitAdd.disabled = true;
  ui.submitAdd.textContent = "添加中…";
  ui.addError.hidden = true;
  try {
    const data = await requestApi("POST", "groups/add", {
      group_id: groupId,
      remark,
    });
    closeAddDialog();
    showToast(data.message || "已添加", "success");
    if (Array.isArray(data.warnings) && data.warnings.length) {
      showToast(data.warnings[0], "info");
    }
    await loadGroups();
  } catch (error) {
    showAddError(error.message);
  } finally {
    ui.submitAdd.disabled = false;
    ui.submitAdd.textContent = "添加";
  }
}

ui.grid.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest(".group-card");
  if (!card) return;
  if (button.dataset.action === "save") {
    void saveGroup(card);
  } else if (button.dataset.action === "reset") {
    void resetGroup(card);
  }
});

ui.grid.addEventListener("input", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (
    target.matches('[data-field="probability-range"]') ||
    target.matches('[data-field="probability-number"]')
  ) {
    syncProbability(target);
  }
});

ui.search.addEventListener("input", applyFilter);
ui.refresh.addEventListener("click", () => void loadGroups({ refresh: true }));
ui.openAdd.addEventListener("click", openAddDialog);
ui.emptyAdd.addEventListener("click", openAddDialog);
ui.closeAdd.addEventListener("click", closeAddDialog);
ui.cancelAdd.addEventListener("click", closeAddDialog);
ui.addForm.addEventListener("submit", (event) => void submitAdd(event));

ui.dialog.addEventListener("click", (event) => {
  if (event.target === ui.dialog) {
    closeAddDialog();
  }
});

async function boot() {
  if (!page) {
    showToast(
      "未找到 AstrBot 插件页面桥接脚本，请从 AstrBot 插件详情页打开本页面。",
      "error",
    );
    ui.resultCount.textContent = "页面桥接未加载";
    return;
  }
  applyTheme(page.getContext());
  if (typeof page.onContext === "function") {
    page.onContext(applyTheme);
  }
  try {
    await Promise.race([
      page.ready(),
      new Promise((_, reject) => {
        window.setTimeout(() => {
          reject(new Error("插件页面桥接初始化超时，请刷新页面重试。"));
        }, 8000);
      }),
    ]);
  } catch (error) {
    showToast(error.message, "error");
    ui.resultCount.textContent = "页面桥接未就绪";
    return;
  }
  await loadGroups();
}

void boot();
