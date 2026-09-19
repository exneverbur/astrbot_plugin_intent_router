/** 意图路由看板。

 运行在 AstrBot 插件 Page 的 sandbox iframe 里，只能通过 window.AstrBotPluginPage
  bridge 调插件后端接口（和「虚拟世界」编辑器同一套写法）。
 */

const bridge = window.AstrBotPluginPage;

/** 核心参数：按用途分组，每项都有悬停说明（label 后面的 ? 上停一下）。 */
const PARAM_GROUPS = [
  {
    title: "正常回复",
    hint: "她在群里接话的那条路——判断模型说「值得回」时走这里。",
    items: [
      [
        "reply_willingness_line",
        "意愿降级线",
        "她的意愿低于这个值时，不拒绝，只改成「排队等一会儿再回」。默认 0.10。\n" +
          "调高：更容易出现慢半拍的回复；调低：意愿低时也秒回。",
      ],
      [
        "reply_queue_delay",
        "低意愿排队延迟（秒）",
        "意愿低于上面那条线时，先压这么久再放行（一轮里多条被合并时，也算这里）。默认 30。\n" +
          "调大：更像「懒得马上回」，但话题可能已经过去了。",
      ],
      [
        "reply_cooldown_seconds",
        "两次开口的最小间隔（秒）",
        "同一条会话里，距上一次真的开口至少隔这么久，下一波消息要排到间隔之后再放行。默认 60。\n" +
          "调大：群里刷屏时她更不容易连着回；调 0：不限制。",
      ],
    ],
  },
  {
    title: "主动插嘴",
    hint: "完全没在跟她说话、但确实是个好梗时才考虑。概率故意做得很低，插嘴多了会很吵。",
    items: [
      [
        "base_p",
        "插嘴基础概率",
        "每条「好梗」消息的起始插嘴概率。默认 0.02（2%）。\n" +
          "想让她更爱插话就把它和下面的上限一起调大。",
      ],
      [
        "hard_cap",
        "插嘴概率上限",
        "算法算出来的概率再高也不会超过这个值（还会被密度和熔断继续压）。默认 0.08。",
      ],
      [
        "rare_threshold",
        "好梗门槛",
        "判断模型给这条消息的「这梗值不值得插一句」分数（0~1）低于它就放弃。默认 0.85。\n" +
          "调低：更容易被逗得插话。",
      ],
      [
        "confidence_threshold",
        "把握门槛",
        "模型对自己判断的自信度低于它就不插嘴。默认 0.90。调低：允许她凭直觉插话。",
      ],
      [
        "willingness_floor",
        "意愿熔断线",
        "她的意愿低于它时插嘴直接归零（只影响插嘴，正常回复不受影响）。默认 0.20。",
      ],
    ],
  },
  {
    title: "密度惩罚",
    hint: "她自己最近说得多不多。这是一段乘在概率上的系数：说得越密，概率越低。",
    items: [
      [
        "penalty_base",
        "密度惩罚底数",
        "每多一份「最近说过话」的加权量，概率就乘一次这个数。默认 0.5：说一次折半。\n" +
          "调小：她收敛得更快、更安静。",
      ],
      [
        "load_penalty_floor",
        "密度惩罚下限",
        "上面那套惩罚最多把她压到这么低，不会压到 0。默认 0.20。调小：话密时更安静。",
      ],
      [
        "load_penalty_slope",
        "群活跃度斜率",
        "拿群本身的热闹程度当分母：群里本来就话多，她同样的发言量罚得就轻。默认 2.0。\n" +
          "调大：越热闹越不罚（冷群里她更安静）。",
      ],
      [
        "silence_bonus_cap",
        "沉默补偿上限",
        "很久没开口时，给插嘴概率加一点补偿，最多加这个比例。默认 0.10（+10%）。",
      ],
    ],
  },
  {
    title: "时间衰减",
    hint: "「最近」到底算多久：把她的发言按离现在的时间折成权重，越近越重。半衰期 = 过了这么久权重剩一半。",
    items: [
      ["half_life_short", "短半衰期（分钟）", "默认 5：5 分钟前的发言权重只剩一半。"],
      ["half_life_mid", "中半衰期（分钟）", "默认 30。"],
      ["half_life_long", "长半衰期（分钟）", "默认 360（6 小时）。调小：只有刚说的话才算数。"],
      [
        "mix_short",
        "短尺度权重",
        "三档衰减按这个比例混合，三个数不必加起来等于 1（会自己归一化）。默认 0.50。",
      ],
      ["mix_mid", "中尺度权重", "默认 0.30。"],
      ["mix_long", "长尺度权重", "默认 0.20。想让「刚刚说过」更管用，就把短的调大。"],
    ],
  },
  {
    title: "硬熔断",
    hint: "硬性次数上限：在窗口内开口次数到顶就完全不再说话。只挡主动插嘴，别人 @ 她照常回。",
    items: [
      ["breaker_short_count", "熔断：短时间内上限（次）", "默认 2 次。填 0 = 关掉这档。"],
      ["breaker_short_window", "熔断窗口：短（秒）", "上面那档统计多久之内。默认 600 秒（10 分钟）。"],
      ["breaker_mid_count", "熔断：中等时间上限（次）", "默认 5 次。填 0 = 关掉这档。"],
      ["breaker_mid_window", "熔断窗口：中（秒）", "默认 3600 秒（1 小时）。"],
      ["breaker_long_count", "熔断：长时间上限（次）", "默认 12 次。填 0 = 关掉这档。"],
      ["breaker_long_window", "熔断窗口：长（秒）", "默认 86400 秒（24 小时）。"],
    ],
  },
  {
    title: "与虚拟世界的联动",
    hint: "意愿值就是「她现在想不想说话」。装了「虚拟世界」就用她的真实状态，没装就用固定值。",
    items: [
      ["willingness_default", "固定意愿（没装虚拟世界时）", "默认 0.55。0 = 完全不想说，1 = 很想说。"],
      ["vm_timeout", "读意愿超时（秒）", "从虚拟世界取意愿最多等这么久，超时先用固定值顶上，不会卡住消息。默认 0.5。"],
      ["vm_breaker_threshold", "连续失败几次熔断", "连续失败这么多次后，暂时不再去问虚拟世界。默认 5 次。"],
      ["vm_breaker_cooldown", "熔断多久后重试（秒）", "熔断后隔这么久再试一次。默认 60。"],
    ],
  },
  {
    title: "记录",
    hint: "存在 router.db 里的数据能留多久。",
    items: [
      [
        "keep_days",
        "记录保留天数",
        "判定流水（以及她在群里说过的话的记录）保留多久，插件启动时清掉过期的。默认 30 天。\n" +
          "看板上的分布图只统计所选窗口内的数据。",
      ],
    ],
  },
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
  box.appendChild(metric("合并未单独回", byDecision.merged || 0, "同一批里并进上面那次"));
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

/** 批量攒批的当前设置：条数 / 最长等待 / 自适应怎么挪。 */
function batchLine(batch) {
  const info = batch || {};
  if (!info.enabled) return "关（每条立刻判）";
  const span = Number(info.interval || 0);
  const low = Number(info.interval_min || 0);
  const high = Math.min(low * 2, span);
  const step = Number(info.interval_step || 0);
  const adaptive =
    info.adaptive && span > 0
      ? `，首条等 ${low}~${high}s，之后每条 +≤${step}s（最多到 ${span}s）`
      : `，固定等 ${span}s`;
  return `开（攒够 ${info.size} 条或${adaptive}）`;
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
    `批量：${batchLine(status.batch)}`,
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

/** 一个小问号：鼠标停上去（或键盘聚焦）弹出说明，靠近右边时会自动往左展开。 */
function helpDot(text) {
  const dot = el("span", "help-dot", "?");
  dot.setAttribute("data-tip", text);
  dot.setAttribute("title", text);
  dot.setAttribute("tabindex", "0");
  dot.setAttribute("role", "note");
  dot.addEventListener("mouseenter", () => {
    const rect = dot.getBoundingClientRect();
    dot.dataset.side = rect.left + rect.width / 2 > window.innerWidth / 2 ? "right" : "left";
  });
  return dot;
}

function paramField(key, label, hint) {
  const field = el("label", "field");
  const head = el("span", "field-head");
  head.appendChild(el("span", "field-name", label));
  head.appendChild(helpDot(hint));
  field.appendChild(head);
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
  return field;
}

function renderParams() {
  const box = $("params-form");
  box.innerHTML = "";
  PARAM_GROUPS.forEach((group) => {
    const section = el("section", "param-group");
    const head = el("div", "param-group-head");
    head.appendChild(el("span", "param-group-title", group.title));
    if (group.hint) head.appendChild(helpDot(group.hint));
    section.appendChild(head);
    const grid = el("div", "params");
    group.items.forEach(([key, label, hint]) => {
      grid.appendChild(paramField(key, label, hint));
    });
    section.appendChild(grid);
    box.appendChild(section);
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
