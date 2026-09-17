/** 意图路由看板。

 运行在 AstrBot 插件 Page 的 sandbox iframe 里，只能通过 window.AstrBotPluginPage
  bridge 调插件后端接口（和「虚拟世界」编辑器同一套写法）。
 */

const bridge = window.AstrBotPluginPage;

const PARAMS = [
  ["base_p", "主动插嘴基础概率", "每条「好梗」的起始概率，默认 0.02"],
  ["hard_cap", "主动插嘴概率上限", "再怎么算也不会超过这个概率，默认 0.08"],
  ["rare_threshold", "好梗门槛", "rare_interject_score 低于它就完全不考虑插嘴，默认 0.85"],
  ["confidence_threshold", "把握门槛", "模型自信度低于它就不插嘴，默认 0.90"],
  ["willingness_floor", "意愿熔断线", "她意愿低于它时直接不插嘴，默认 0.20"],
  ["reply_willingness_line", "意愿降级线", "低于它就不秒回、排队延迟，默认 0.10"],
  ["penalty_base", "密度惩罚底数", "0.5 表示每加权一次发言折半，默认 0.5"],
  ["load_penalty_floor", "密度惩罚下限", "再密也不会低于这个值，默认 0.20"],
  ["load_penalty_slope", "密度惩罚斜率", "bot 发言占比放大倍数，默认 2.0"],
  ["silence_bonus_cap", "沉默补偿上限", "太久没说话给的一点加成，默认 0.10"],
  ["half_life_short", "短半衰期（分钟）", "默认 5"],
  ["half_life_mid", "中半衰期（分钟）", "默认 30"],
  ["half_life_long", "长半衰期（分钟）", "默认 360"],
  ["mix_short", "短尺度权重", "默认 0.50"],
  ["mix_mid", "中尺度权重", "默认 0.30"],
  ["mix_long", "长尺度权重", "默认 0.20"],
  ["breaker_short_count", "熔断：近 10 分钟上限", "默认 2 次"],
  ["breaker_mid_count", "熔断：近 1 小时上限", "默认 5 次"],
  ["breaker_long_count", "熔断：近 24 小时上限", "默认 12 次"],
  ["reply_queue_delay", "低意愿排队延迟（秒）", "默认 30"],
  ["willingness_default", "没装 VM 时的固定意愿", "默认 0.55"],
  ["vm_timeout", "VM 超时（秒）", "默认 0.5"],
  ["vm_breaker_threshold", "VM 连续失败几次熔断", "默认 5"],
  ["vm_breaker_cooldown", "VM 熔断时长（秒）", "默认 60"],
  ["keep_days", "记录保留天数", "默认 30"],
];

const ui = { token: "", stats: {}, status: {}, params: {}, session: "" };

function $(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.remove("hidden");
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => box.classList.add("hidden"), 3200);
}

async function apiGet(endpoint, params = {}) {
  return bridge.apiGet(endpoint, { ...params, token: ui.token });
}

async function apiPost(endpoint, body = {}) {
  return bridge.apiPost(endpoint, { ...body, token: ui.token });
}

function formatTime(ts) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return "";
  const date = new Date(value * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}`;
}

function metric(label, value, sub) {
  const box = el("div", "metric");
  box.appendChild(el("div", "label", label));
  box.appendChild(el("div", "value", String(value)));
  if (sub) box.appendChild(el("div", "sub", sub));
  return box;
}

function renderChart(container, dist) {
  container.innerHTML = "";
  const entries = Object.entries(dist || {});
  const total = entries.reduce((sum, [, count]) => sum + Number(count || 0), 0) || 1;
  entries.forEach(([label, count]) => {
    const row = el("div", "chart-row");
    row.appendChild(el("span", "muted", label));
    const bar = el("div", "bar");
    const fill = document.createElement("i");
    fill.style.width = `${Math.round((Number(count || 0) / total) * 100)}%`;
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", "muted", String(count)));
    container.appendChild(row);
  });
}

function renderCards(report) {
  const box = $("cards");
  box.innerHTML = "";
  const counts = report.counts || {};
  const byDecision = counts.by_decision || {};
  const proactive = report.proactive || {};
  const counters = report.counters || {};
  box.appendChild(metric("判断条数", counts.judged || 0, `窗口 ${Math.round((report.window_seconds || 0) / 86400)} 天`));
  box.appendChild(metric("放行回复", byDecision.reply || 0, "worth=true 正常放行"));
  box.appendChild(metric("主动插嘴", byDecision.proactive || 0, `候选 ${proactive.candidates || 0} 条`));
  box.appendChild(metric("熔断/不回", `${byDecision.blocked || 0} / ${byDecision.ignore || 0}`));
  box.appendChild(metric("判断调用", counters.judge_calls || 0, `失败 ${counters.judge_failed || 0}`));
  box.appendChild(metric("缓存命中", counters.cache_hits || 0));
  box.appendChild(
    metric(
      "tokens",
      counters.total_tokens || 0,
      `输入 ${counters.prompt_tokens || 0} / 输出 ${counters.completion_tokens || 0}`,
    ),
  );
  box.appendChild(metric("误判标记", `${counts.feedback?.good || 0} 👍 / ${counts.feedback?.bad || 0} 👎`));
}

function scoreText(scores) {
  if (!scores) return "";
  const parts = [
    ["回", scores.reply_score],
    ["梗", scores.rare_interject_score],
    ["信", scores.confidence],
    ["相关", scores.relevance_to_bot],
  ];
  return parts
    .filter(([, value]) => Number(value) > 0)
    .map(([name, value]) => `${name}${Number(value).toFixed(2)}`)
    .join(" ");
}

function renderJudgements(rows) {
  const body = $("judgement-body");
  body.innerHTML = "";
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "muted", "还没有判定记录。");
    td.colSpan = 9;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = el("tr");
    tr.appendChild(el("td", "muted", String(row.id)));
    tr.appendChild(el("td", "muted", formatTime(row.ts)));
    tr.appendChild(el("td", "muted", String(row.umo || "").replace(/^.*:/, "")));
    tr.appendChild(el("td", "", row.text || ""));
    tr.appendChild(el("td", "muted", scoreText(row.scores)));
    const decision = el("td");
    decision.appendChild(el("span", `tag ${row.decision}`, row.decision || ""));
    tr.appendChild(decision);
    tr.appendChild(el("td", "muted", Number(row.probability || 0).toFixed(3)));
    tr.appendChild(el("td", "muted", row.reason || ""));
    const actions = el("td");
    [
      ["👍", "good"],
      ["👎", "bad"],
    ].forEach(([icon, value]) => {
      const button = el("button", "small", icon);
      if (row.feedback === value) button.classList.add("primary");
      button.addEventListener("click", () => sendFeedback(row.id, value));
      actions.appendChild(button);
    });
    tr.appendChild(actions);
    body.appendChild(tr);
  });
}

async function sendFeedback(id, value) {
  try {
    await apiPost("feedback", { id, value });
    toast("已记下，看板统计里能看到");
    loadStats();
  } catch (error) {
    toast(error.message || "标记失败");
  }
}

function renderStatus() {
  const status = ui.status || {};
  const provider = status.willingness_provider || {};
  $("enable-toggle").checked = Boolean(status.enable);
  $("proactive-toggle").checked = Boolean(status.proactive_enabled);
  const bits = [
    `判断模型：${status.judge_provider_id || "（会话默认）"}`,
    `意愿来源：${provider.mode || "-"}`,
    provider.breaker_open ? `VM 熔断中（还剩 ${provider.breaker_seconds}s）` : "VM 正常",
    status.vm_linked ? "已接上虚拟世界" : "没装虚拟世界（用固定意愿）",
    `批量：${status.batch?.enabled ? `开（${status.batch.size} 条 / ${status.batch.interval}s）` : "关"}`,
    `记录：发言 ${status.storage?.speech || 0} 条 / 判定 ${status.storage?.judgement || 0} 条`,
    provider.last_error ? `最近一次回落：${provider.last_error}` : "",
  ].filter(Boolean);
  $("status-line").textContent = bits.join("　·　");
  $("data-line").textContent = `库里的记录：她说过 ${status.storage?.speech || 0} 次，判定 ${status.storage?.judgement || 0} 条。`;

  const select = $("session");
  const previous = ui.session;
  select.innerHTML = "";
  select.appendChild(el("option", "", "全部会话")).value = "";
  (status.sessions || []).forEach((umo) => {
    const option = document.createElement("option");
    option.value = umo;
    option.textContent = umo;
    select.appendChild(option);
  });
  select.value = (status.sessions || []).includes(previous) ? previous : "";
  ui.session = select.value;
}

function renderParams() {
  const box = $("params-form");
  box.innerHTML = "";
  PARAMS.forEach(([key, label, hint]) => {
    const field = el("label", "field");
    field.title = hint;
    field.appendChild(el("span", "", label));
    const value = ui.params[key];
    if (typeof value === "boolean") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = value;
      input.dataset.key = key;
      field.appendChild(input);
    } else {
      const input = document.createElement("input");
      input.type = "number";
      input.step = Number.isInteger(value) ? "1" : "0.01";
      input.value = value;
      input.dataset.key = key;
      field.appendChild(input);
    }
    box.appendChild(field);
  });
}

async function saveParams() {
  const params = {};
  $("params-form")
    .querySelectorAll("input[data-key]")
    .forEach((input) => {
      params[input.dataset.key] = input.type === "checkbox" ? input.checked : Number(input.value);
    });
  try {
    const result = await apiPost("params", { params });
    ui.params = result.params || params;
    renderParams();
    toast("参数已保存并生效");
  } catch (error) {
    toast(error.message || "保存失败");
  }
}

async function resetParams() {
  try {
    const result = await apiPost("params", { reset: true });
    ui.params = result.params || {};
    renderParams();
    toast("已恢复默认参数");
  } catch (error) {
    toast(error.message || "恢复失败");
  }
}

async function loadStatus() {
  ui.status = await apiGet("status");
  renderStatus();
}

async function loadStats() {
  const windowSeconds = $("window").value;
  const params = { window: windowSeconds, limit: 80 };
  if (ui.session) params.session = ui.session;
  const report = await apiGet("stats", params);
  ui.stats = report;
  renderCards(report);
  renderChart($("willingness-chart"), report.willingness_dist);
  renderChart($("penalty-chart"), report.density_penalty_dist);
  renderJudgements(report.recent || []);
}

async function loadParams() {
  const result = await apiGet("params");
  ui.params = result.params || {};
  renderParams();
}

async function refreshAll() {
  try {
    await Promise.all([loadStatus(), loadParams()]);
    await loadStats();
  } catch (error) {
    toast(error.message || "读取失败");
  }
}

async function control(body) {
  try {
    const result = await apiPost("control", body);
    ui.status = result.status || ui.status;
    renderStatus();
    toast((result.changed || []).join("；") || "已更新");
  } catch (error) {
    toast(error.message || "操作失败");
  }
}

async function exportData() {
  try {
    const params = ui.session ? { session: ui.session } : {};
    await bridge.download("data", params, "intent-router-export.json");
  } catch (error) {
    toast(error.message || "导出失败");
  }
}

async function clearData() {
  if (!window.confirm("清空所有发言记录与判定流水？这个不能撤销。")) return;
  try {
    await apiPost("data", { clear: true });
    toast("已清空");
    refreshAll();
  } catch (error) {
    toast(error.message || "清空失败");
  }
}

function boot() {
  $("refresh").addEventListener("click", refreshAll);
  $("window").addEventListener("change", loadStats);
  $("session").addEventListener("change", () => {
    ui.session = $("session").value;
    loadStats();
  });
  $("enable-toggle").addEventListener("change", (event) =>
    control({ enable: event.target.checked }),
  );
  $("proactive-toggle").addEventListener("change", (event) =>
    control({ proactive_enabled: event.target.checked }),
  );
  $("params-save").addEventListener("click", saveParams);
  $("params-reset").addEventListener("click", resetParams);
  $("data-export").addEventListener("click", exportData);
  $("data-prune").addEventListener("click", async () => {
    try {
      const result = await apiPost("data", { prune: true });
      toast(`已清理 ${result.removed || 0} 条过期记录`);
      refreshAll();
    } catch (error) {
      toast(error.message || "清理失败");
    }
  });
  $("data-clear").addEventListener("click", clearData);
  refreshAll();
  window.setInterval(() => {
    if (!document.hidden) refreshAll();
  }, 15000);
}

boot();
