"""AstrBot 智能意图路由插件（Intent Router）

职责：
- 只监听群聊消息，判断哪些消息"值得 AI 主人格主动回复"（私聊完全不参与）；
- 只做意图判断与消息路由，不生成任何回复内容；
- 通过低成本 LLM（judge_provider，面板下拉选择，Provider 自带模型）批量判断，控制 Token 消耗；
- 值得回复的群消息通过事件队列重注入（带 At 唤醒标记），交给主人格正常回复。
"""

import asyncio
import copy
import json
import random
import re
import time
import uuid
from collections import deque
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.star import Context, Star, register

# handler 优先级：高于主人格（默认 0），低于框架内部 maxsize 级别的控制 handler。
# 如需调整，可改这里（AstrBot 的 handler 按 priority 从高到低执行）。
HANDLER_PRIORITY = 100

# 重注入事件标记：避免被本插件再次判断造成循环
REINJECT_EXTRA = "intent_router_judged"

# 内置判断 Prompt。{persona} 会替换为配置中的 persona（简略人设）。
# 配置 judge_prompt 后完全覆盖本 Prompt，仍可使用 {persona} 占位符。
DEFAULT_JUDGE_PROMPT = (
    "你是一个群聊消息判断器。下面会给出一个群聊中最近的消息背景和一批待判断消息，"
    "请判断其中哪些消息值得 AI 助手主动回复。\n"
    "\n"
    "AI 助手的人设（简略）：{persona}\n"
    "\n"
    "值得回复（worth=true）：\n"
    "- 用户提出具体问题（知识、技术、建议、预测等）\n"
    "- 用户请求帮助或解释\n"
    "- 用户分享想法/经历，AI 可补充信息或给出观点\n"
    "- 用户表达情绪，AI 可适当共情或安慰\n"
    "- 其他符合该人设、值得 AI 主动参与的话题\n"
    "\n"
    "不值得回复（worth=false）：\n"
    "- 日常社交邀约（吃饭、出去玩、见面等）\n"
    "- 纯闲聊无信息量（吃了没、在干嘛等）\n"
    "- 打听他人隐私（年龄、收入、住址等）\n"
    "- 纯感叹/重复（太好了、哈哈等）\n"
    "- 与 AI 能力无关且无需 AI 参与的内容\n"
    "\n"
    "要求：\n"
    "1. 待判断消息的序号从 1 开始，与消息列表一一对应。\n"
    "2. 每条消息自行推断\"谁对谁说\"：from 为发送者（抄自消息列表）；to 为该消息可能的接收对象"
    "（\"机器人\"/某个用户的昵称/\"大家\"/\"不确定\"）；directed 表示是否明确在对机器人'我'说话。\n"
    "3. 对每条消息写一句简短原因（reason，不超过 15 个字），再给出判断结果。\n"
    "4. 只输出一个 JSON 对象，不要输出任何解释或其他内容。\n"
    '5. 输出格式：{"results":[{"idx":1,"from":"张三","to":"机器人","directed":true,"reason":"明确向机器人提问","worth":true,"confidence":0.9},'
    '{"idx":2,"from":"李四","to":"王五","directed":false,"reason":"两人之间的对话","worth":false,"confidence":0.8}]}\n'
    "6. confidence 是 0~1 的小数，表示你对该判断的把握程度。\n"
    "7. 拿不准时优先 worth=false。"
)

# 指向性判断规则（追加在系统提示词中）
DIRECTED_RULES = (
    "\n"
    "指向性判断（谁对谁说）：\n"
    "- 群聊中人们通常不会特意使用\"回复\"功能，消息列表里的\"(回复 xxx)\"标记仅供参考，不是唯一依据。\n"
    "- 请根据消息内容与上下文自行推断每条消息可能是在对谁说（to 字段）：例如提到/接续某人的话题、"
    "回应某人的问题、直接问机器人、或者只是对大家说。\n"
    "- to 为\"机器人\"或内容明显在对机器人说话时（directed=true），默认值得回复，除非明显不友好或纯噪音。\n"
    "- to 为某个具体用户时（两人之间的对话），默认不值得回复，除非 AI 能明显补充价值。\n"
    "- to 为\"大家\"或\"不确定\"时，按上文的值得/不值得标准判断。\n"
    "- 某个话题已在多人之间连续讨论多轮、且机器人从未参与时，属于人类之间的持续对话，"
    "默认不要中途插入回复。\n"
)

# 纯语气词/笑声（哈哈哈、嘿嘿、呵呵、hahaha、hehe、hhhh 等）
_LAUGHTER_RE = re.compile(
    r"^(?:(?:哈|嘻|嘿|呵)+|(?:ha+|he+|hi+|h+)+)$",
    re.IGNORECASE,
)

# 常用 emoji 范围（用于"纯表情"噪音识别）
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 主要表情符号区
    "\U0001F1E6-\U0001F1FF"  # 国旗
    "\u2600-\u27BF"  # 杂项符号/装饰符号
    "\u2B00-\u2BFF"  # 箭头/杂项符号
    "\u2190-\u21FF"  # 箭头
    "\uFE0F\u200D\u2764\u2B50"
    "]"
)

# 有效字符：中文、字母、数字
_EFFECTIVE_CHAR_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")


def _cfg_str(config: AstrBotConfig, key: str, default: str) -> str:
    v = config.get(key, default)
    return str(v) if v is not None else default


def _cfg_bool(config: AstrBotConfig, key: str, default: bool) -> bool:
    v = config.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "开启", "是")
    return bool(v)


def _cfg_int(config: AstrBotConfig, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except Exception:
        return default


def _cfg_float(config: AstrBotConfig, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except Exception:
        return default


def _cfg_list(config: AstrBotConfig, key: str, default: list) -> list:
    v = config.get(key, default)
    return list(v) if isinstance(v, (list, tuple)) else default


@register("astrbot_plugin_intent_router", "Codex", "智能意图路由：判断哪些消息值得主人格主动回复", "0.1.0")
class IntentRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)

        self.enable = _cfg_bool(config, "enable", True)
        self.judge_provider_id = _cfg_str(config, "judge_provider", "").strip()
        self.bot_persona = _cfg_str(config, "persona", "乐于助人的AI助手").strip()
        self.judge_prompt = _cfg_str(config, "judge_prompt", "").strip()

        self.enable_batch = _cfg_bool(config, "enable_batch", True)
        self.batch_size = max(1, _cfg_int(config, "batch_size", 5))
        self.batch_interval = max(1, _cfg_int(config, "batch_interval", 30))
        self.adaptive = _cfg_bool(config, "adaptive", True)
        self.interval_min = max(1, _cfg_int(config, "interval_min", 5))
        self.interval_step = max(0, _cfg_int(config, "interval_step", 5))
        if self.interval_min > self.batch_interval:
            self.interval_min = self.batch_interval
        self.max_batch = max(1, _cfg_int(config, "max_batch", 20))
        self.buffer_max_capacity = max(1, _cfg_int(config, "buffer_cap", 100))
        self.buffer_max_age = max(1, _cfg_int(config, "buffer_age", 60))

        self.confidence_threshold = min(
            1.0, max(0.0, _cfg_float(config, "threshold", 0.5))
        )

        # 与「虚拟世界」插件联动：她在那里越有社交欲，放行阈值越低（越愿意接话）
        self.link_virtual_world = _cfg_bool(config, "link_virtual_world", True)
        self.vw_threshold_min = min(
            1.0, max(0.0, _cfg_float(config, "vw_threshold_min", 0.3))
        )
        self.vw_threshold_max = min(
            1.0, max(0.0, _cfg_float(config, "vw_threshold_max", 0.75))
        )
        self.vw_cache_seconds = max(1, _cfg_int(config, "vw_cache_seconds", 10))
        self._vw_threshold_cache: dict[str, tuple[float, float]] = {}
        self._vw_link_logged = False
        self._vw_sleep_logged = False
        self.release_policy = _cfg_str(config, "release", "latest").strip().lower()
        if self.release_policy not in ("latest", "all"):
            self.release_policy = "latest"
        self.failure_policy = _cfg_str(config, "fail_mode", "block").strip().lower()
        if self.failure_policy not in ("block", "pass"):
            self.failure_policy = "block"

        self.min_length = max(0, _cfg_int(config, "min_length", 2))
        self.noise_words = _cfg_list(config, "noise_words", ["打卡", "签到", "冒泡", "收到", "顶", "+1"])
        self.pass_prefixes = _cfg_list(config, "pass_prefix", [])
        self.bot_aliases = [
            str(a).strip().lower()
            for a in _cfg_list(config, "bot_aliases", [])
            if str(a).strip()
        ]

        self.enable_cache = _cfg_bool(config, "cache", True)
        self.cache_ttl = max(1, _cfg_int(config, "cache_ttl", 300))
        self.cache_max_entries = max(10, _cfg_int(config, "cache_cap", 2000))
        self.context_window = max(0, _cfg_int(config, "context_len", 10))

        self.groups_whitelist = _cfg_list(config, "whitelist", [])
        self.groups_blacklist = _cfg_list(config, "blacklist", [])

        self.json_mode = _cfg_bool(config, "json_mode", False)
        self.temperature = _cfg_float(config, "temperature", 0.0)
        self.max_tokens = max(1, _cfg_int(config, "max_tokens", 256))
        self.judge_concurrency = max(1, _cfg_int(config, "concurrency", 4))
        self.debug = _cfg_bool(config, "debug", False)
        self.other_plugins_priority = _cfg_bool(config, "passthrough", False)

        # 运行时状态
        self._buffers: dict[str, deque] = {}
        self._buffer_lock = asyncio.Lock()
        self._history: dict[str, deque] = {}
        self._deadlines: dict[str, float] = {}
        self._cache: dict[tuple, dict] = {}
        self._judge_semaphore = asyncio.Semaphore(self.judge_concurrency)
        self._flush_task: Optional[asyncio.Task] = None
        self._seq = 0
        self._stats = {
            "noise_blocked": 0,
            "cache_hits": 0,
            "judge_calls": 0,
            "batch_judged": 0,
            "blocked": 0,
            "released": 0,
            "released_candidate": 0,
            "dropped_stale": 0,
            "dropped_overflow": 0,
            "reinject_failed": 0,
            "skipped_other_plugin_reply": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    # ------------------------------------------------------------------
    # 消息入口
    # ------------------------------------------------------------------
    # 只监听群聊消息；私聊消息不会进入本插件处理逻辑
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=HANDLER_PRIORITY)
    async def on_message(self, event: AstrMessageEvent) -> None:
        try:
            await self._route(event)
        except Exception as e:
            logger.error(f"intent_router 处理消息异常: {e}", exc_info=True)
            # 兜底：不拦截，让消息继续走后续插件/主人格

    async def _route(self, event: AstrMessageEvent) -> None:
        if not self.enable:
            return
        # 机器人自己的消息（平台回显等）不处理
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        # 本插件重注入的消息：直接放行，避免循环
        if event.get_extra(REINJECT_EXTRA, False):
            return

        text = self._clean_text(event.message_str or "")
        is_group = not event.is_private_chat()

        # 群聊消息写入背景历史（供批量判断参考）
        item_uuid = uuid.uuid4().hex if (is_group and text) else None
        if is_group and text:
            self._history_append(event, text, item_uuid)

        # 兜底：私聊不处理（正常由 GROUP_MESSAGE 过滤器保证私聊消息不会进入本 handler）
        if event.is_private_chat():
            return

        # 直接指向 bot / 唤醒 / 命令：直接放行
        if self._is_direct(event):
            return

        if is_group and not self._in_scope(event):
            return

        if not text:
            # 纯图片等无文本消息：不判断，放行
            return

        if self._is_noise(text):
            self._stats["noise_blocked"] += 1
            self._block_event(event)
            return

        cache_key = (event.unified_msg_origin, text)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._stats["cache_hits"] += 1
            if cached["worth"]:
                self._mark_wake(event)
                self._stats["released"] += 1
            else:
                self._block_event(event)
            return

        if self.enable_batch and is_group:
            self._buffer_add(event, text, cache_key, item_uuid)
            self._block_event(event)
            return

        # 单条即时判断（enable_batch=false 时）
        item = self._make_item(event, text, cache_key, item_uuid)
        await self._judge_single(event, item)

    # ------------------------------------------------------------------
    # 分流判断
    # ------------------------------------------------------------------
    def _is_direct(self, event: AstrMessageEvent) -> bool:
        """直接指向 bot 的消息（@、回复、唤醒前缀、命令）→ 放行。"""
        if event.is_at_or_wake_command:
            return True
        if event.get_extra("handlers_parsed_params", {}):
            return True
        s = event.message_str or ""
        for p in self.pass_prefixes:
            if p and s.startswith(p):
                return True
        if self.bot_aliases:
            sl = s.lower()
            for alias in self.bot_aliases:
                if alias and alias in sl:
                    return True
        return False

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        if self.groups_whitelist and gid not in self.groups_whitelist:
            return False
        if gid and gid in self.groups_blacklist:
            return False
        return True

    def _is_noise(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        # 纯表情
        if _EMOJI_RE.sub("", t).strip() == "":
            return True
        # 纯标点/符号
        effective = _EFFECTIVE_CHAR_RE.findall(t)
        if not effective:
            return True
        # 纯语气词/笑声
        if _LAUGHTER_RE.match(t):
            return True
        # 有效字符数不足
        if len(effective) < self.min_length:
            return True
        # 单字符重复（1111、好好好好）
        if len(t) >= 2 and len(set(t)) == 1:
            return True
        # 无信息量词汇（精确匹配）
        if t in self.noise_words:
            return True
        return False

    def _mark_wake(self, event: AstrMessageEvent) -> None:
        """将事件标记为唤醒，使主 LLM agent 在管道末尾正常回复。"""
        event.is_wake = True
        event.is_at_or_wake_command = True

    def _block_event(self, event: AstrMessageEvent) -> None:
        """严格模式：拦截事件，后续插件不再处理。
        透传模式（passthrough=true）：不拦截，让其他插件也能处理。
        """
        if not self.other_plugins_priority:
            event.stop_event()

    # ------------------------------------------------------------------
    # 批量缓冲与刷新
    # ------------------------------------------------------------------
    def _buffer_add(self, event: AstrMessageEvent, text: str, cache_key: tuple, item_uuid: str) -> None:
        umo = event.unified_msg_origin
        buf = self._buffers.setdefault(umo, deque())
        was_empty = not buf
        item = self._make_item(event, text, cache_key, item_uuid)
        buf.append(item)
        if self.adaptive:
            if was_empty:
                # 随机初始等待：interval_min ~ 2*interval_min（不超过 batch_interval）
                initial = random.uniform(
                    self.interval_min,
                    min(self.interval_min * 2, self.batch_interval),
                )
                self._deadlines[umo] = item["ts"] + initial
            else:
                # 每条新消息随机延长一点，最多不超过 batch_interval（按首条消息计）
                inc = random.uniform(0, self.interval_step)
                deadline = self._deadlines.get(
                    umo, buf[0]["ts"] + self.interval_min
                ) + inc
                self._deadlines[umo] = min(deadline, buf[0]["ts"] + self.batch_interval)
        while len(buf) > self.buffer_max_capacity:
            buf.popleft()
            self._stats["dropped_overflow"] += 1

    def _make_item(self, event: AstrMessageEvent, text: str, cache_key: tuple, item_uuid: Optional[str]) -> dict:
        self._seq += 1
        reply_to = ""
        try:
            msg_obj = getattr(event, "message_obj", None)
            chain = getattr(msg_obj, "message", None)
            if isinstance(chain, list):
                for comp in chain:
                    if isinstance(comp, Reply):
                        nick = str(getattr(comp, "sender_nickname", "") or "").strip()
                        sid = str(getattr(comp, "sender_id", "") or "").strip()
                        reply_to = nick or sid
                        break
        except Exception:
            pass
        return {
            "event": event,
            "text": text,
            "cache_key": cache_key,
            "uuid": item_uuid or uuid.uuid4().hex,
            "ts": time.time(),
            "seq": self._seq,
            "sender_name": self._sender_name(event),
            "reply_to": reply_to,
        }

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                now = time.time()
                due: list[str] = []
                for umo, buf in list(self._buffers.items()):
                    if not buf:
                        continue
                    if len(buf) >= self.batch_size:
                        due.append(umo)
                        continue
                    if self._flush_due(umo, buf, now):
                        due.append(umo)
                for umo in due:
                    try:
                        await self._flush_group(umo)
                    except Exception as e:
                        logger.error(f"intent_router: 刷新缓冲失败 umo={umo}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"intent_router: 后台刷新循环异常: {e}")

    async def _flush_group(self, umo: str) -> None:
        items: list[dict] = []
        async with self._buffer_lock:
            buf = self._buffers.get(umo)
            if not buf:
                return
            now = time.time()
            # 丢弃过期消息，避免回复几十秒前的话题
            while buf and now - buf[0]["ts"] > self.buffer_max_age:
                buf.popleft()
                self._stats["dropped_stale"] += 1
            for _ in range(min(self.max_batch, len(buf))):
                items.append(buf.popleft())
            if not buf:
                self._buffers.pop(umo, None)
                self._deadlines.pop(umo, None)
            else:
                # 有剩余消息，刷新截止时间仍受 batch_interval 上限约束
                self._deadlines[umo] = min(
                    self._deadlines.get(umo, now + self.interval_min),
                    buf[0]["ts"] + self.batch_interval,
                )
        if not items:
            return

        self._stats["judge_calls"] += 1
        self._stats["batch_judged"] += len(items)
        history_lines = self._build_history_lines(umo, items)
        user_prompt = self._build_user_prompt(items, history_lines)
        system_prompt = self._build_system_prompt()
        if self.debug:
            logger.info("intent_router 判断输入:\n%s\n---\n%s", system_prompt, user_prompt)

        parsed = await self._call_judge(system_prompt, user_prompt, umo)
        if parsed is None:
            logger.error(
                f"intent_router: 批量判断失败(umo={umo})，按 fail_mode={self.failure_policy} 处理"
            )
            if self.failure_policy == "pass":
                self._release_items(items)
            else:
                for it in items:
                    self._stats["blocked"] += 1
            return

        by_idx = {r["idx"]: r for r in parsed}
        released: list[dict] = []
        threshold = await self._effective_threshold(umo)
        for i, it in enumerate(items, 1):
            r = by_idx.get(i)
            if r is None:
                self._cache_set(it["cache_key"], False, 1.0)
                self._stats["blocked"] += 1
                if self.debug:
                    logger.info(
                        "intent_router 判断结果: idx=%d 文本=%r 格式异常，按不值得处理",
                        i,
                        it["text"],
                    )
                continue
            self._cache_set(it["cache_key"], r["worth"], r["confidence"])
            if self.debug:
                logger.info(
                    "intent_router 判断结果: idx=%d worth=%s confidence=%.2f from=%s to=%s directed=%s reason=%s 文本=%r",
                    i,
                    r["worth"],
                    r["confidence"],
                    r.get("from", ""),
                    r.get("to", ""),
                    r.get("directed", False),
                    r.get("reason", ""),
                    it["text"],
                )
            if r["worth"] and r["confidence"] >= threshold:
                released.append(it)
            else:
                self._stats["blocked"] += 1
        self._stats["released_candidate"] += len(released)
        self._release_items(released)

    def _flush_due(self, umo: str, buf: deque, now: float) -> bool:
        """判断该群缓冲是否该刷新了。
        规则：满 batch_size 立即刷；超过 batch_interval（硬上限）刷；
        自适应开启时，到达该批的动态截止时间（随机初始值 + 每条新消息随机延长）刷。"""
        if len(buf) >= self.batch_size:
            return True
        if now - buf[0]["ts"] >= self.batch_interval:
            return True
        if not self.adaptive:
            return False
        deadline = self._deadlines.get(umo, buf[0]["ts"] + self.interval_min)
        return now >= deadline

    def _release_items(self, items: list[dict]) -> None:
        if not items:
            return
        if self.release_policy == "latest":
            # 从最新一条开始，遇到已被其他插件回复的则回退到更早的一条
            ordered = sorted(items, key=lambda it: (it["ts"], it["seq"]), reverse=True)
            for it in ordered:
                if self.other_plugins_priority and getattr(
                    it["event"], "_has_send_oper", False
                ):
                    # 其他插件已经回复了这条消息，避免 AI 重复回复
                    self._stats["skipped_other_plugin_reply"] += 1
                    continue
                self._reinject(it["event"])
                self._stats["released"] += 1
                break
        else:
            for it in items:
                if self.other_plugins_priority and getattr(
                    it["event"], "_has_send_oper", False
                ):
                    self._stats["skipped_other_plugin_reply"] += 1
                    continue
                self._reinject(it["event"])
                self._stats["released"] += 1

    def _reinject(self, event: AstrMessageEvent) -> None:
        """把消息副本（带 At 唤醒标记）放回事件队列，重新走一遍管道。"""
        try:
            new_event = copy.copy(event)
            # 原事件可能已被 stop_event()（缓冲时拦截过），副本必须清除
            # 停止/结果状态，否则重注入后会在管道第一站就被丢弃。
            if hasattr(new_event, "_force_stopped"):
                new_event._force_stopped = False
            if hasattr(new_event, "clear_result"):
                new_event.clear_result()
            if hasattr(new_event, "_has_send_oper"):
                new_event._has_send_oper = False
            new_event._extras = dict(getattr(event, "_extras", {}) or {})
            msg_obj = getattr(new_event, "message_obj", None)
            chain = getattr(msg_obj, "message", None)
            self_id = event.get_self_id()
            if isinstance(chain, list):
                has_at_self = any(
                    isinstance(c, At) and str(getattr(c, "qq", "")) == str(self_id)
                    for c in chain
                )
                if not has_at_self:
                    chain.insert(0, At(qq=self_id, name=self_id))
            new_event.set_extra(REINJECT_EXTRA, True)
            self.context.get_event_queue().put_nowait(new_event)
        except Exception as e:
            logger.error(f"intent_router: 重注入消息失败: {e}")
            self._stats["reinject_failed"] += 1

    # ------------------------------------------------------------------
    # 单条即时判断
    # ------------------------------------------------------------------
    async def _judge_single(self, event: AstrMessageEvent, item: dict) -> None:
        self._stats["judge_calls"] += 1
        history_lines = self._build_history_lines(event.unified_msg_origin, [item])
        user_prompt = self._build_user_prompt([item], history_lines)
        system_prompt = self._build_system_prompt()
        if self.debug:
            logger.info("intent_router 判断输入:\n%s\n---\n%s", system_prompt, user_prompt)

        parsed = await self._call_judge(system_prompt, user_prompt, event.unified_msg_origin)
        if parsed is None:
            if self.failure_policy == "pass":
                self._mark_wake(event)
                self._stats["released"] += 1
            else:
                self._block_event(event)
                self._stats["blocked"] += 1
            return

        r = parsed[0] if parsed else None
        threshold = await self._effective_threshold(event.unified_msg_origin)
        if r and r["idx"] == 1 and r["worth"] and r["confidence"] >= threshold:
            self._mark_wake(event)
            self._stats["released"] += 1
            self._cache_set(item["cache_key"], True, r["confidence"])
            if self.debug:
                logger.info(
                    "intent_router 判断结果: 放行 confidence=%.2f from=%s to=%s directed=%s reason=%s 文本=%r",
                    r["confidence"],
                    r.get("from", ""),
                    r.get("to", ""),
                    r.get("directed", False),
                    r.get("reason", ""),
                    item["text"],
                )
        else:
            self._block_event(event)
            self._stats["blocked"] += 1
            self._cache_set(item["cache_key"], False, r["confidence"] if r else 1.0)
            if self.debug:
                logger.info(
                    "intent_router 判断结果: 拦截 confidence=%.2f from=%s to=%s directed=%s reason=%s 文本=%r",
                    r["confidence"] if r else 0.0,
                    r.get("from", "") if r else "",
                    r.get("to", "") if r else "",
                    r.get("directed", False) if r else False,
                    r.get("reason", "") if r else "",
                    item["text"],
                )

    # ------------------------------------------------------------------
    # LLM 调用与解析
    # ------------------------------------------------------------------
    async def _call_judge(
        self,
        system_prompt: str,
        user_prompt: str,
        umo: str,
    ) -> Optional[list[dict]]:
        provider = None
        if self.judge_provider_id:
            provider = self.context.get_provider_by_id(self.judge_provider_id)
            if provider is None:
                logger.warning(
                    f"intent_router: 未找到 judge_provider='{self.judge_provider_id}'，"
                    "回退到会话默认 Provider"
                )
        if provider is None:
            provider = await self.context.get_using_provider_async(umo)
        if provider is None:
            logger.error("intent_router: 没有可用 LLM Provider，无法判断")
            return None

        async with self._judge_semaphore:
            resp = None
            full_kwargs = {"temperature": self.temperature, "max_tokens": self.max_tokens}
            if self.json_mode:
                full_kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = await provider.text_chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    **full_kwargs,
                )
            except Exception as e1:
                # 部分 Provider 不接受 temperature/max_tokens/response_format，降级重试
                logger.debug(f"intent_router: 带参调用失败({e1})，降级为最小参数重试")
                try:
                    resp = await provider.text_chat(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                    )
                except Exception as e2:
                    logger.error(f"intent_router: LLM 调用失败: {e2}")
                    return None
            if resp is None:
                return None

        self._record_usage(resp)
        raw = getattr(resp, "completion_text", "") or ""
        if self.debug:
            logger.info("intent_router 判断输出:\n%s", raw)
        return self._parse_judge_output(raw)

    def _record_usage(self, resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        s = self._stats
        try:
            s["prompt_tokens"] += int(getattr(usage, "input", 0) or 0)
        except Exception:
            pass
        try:
            s["completion_tokens"] += int(getattr(usage, "output", 0) or 0)
        except Exception:
            pass
        try:
            s["total_tokens"] += int(getattr(usage, "total", 0) or 0)
        except Exception:
            pass

    def _parse_judge_output(self, raw: str) -> Optional[list[dict]]:
        """解析 LLM 输出，返回 [{"idx","worth","confidence"}, ...]；失败返回 None。"""
        if not raw or not raw.strip():
            return None
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)

        results: list[dict] = []
        parsed_ok = False
        try:
            data = json.loads(text)
            if isinstance(data, list):
                entries = data
                parsed_ok = True
            elif isinstance(data, dict) and isinstance(data.get("results"), list):
                entries = data["results"]
                parsed_ok = True
            else:
                entries = None
            if parsed_ok:
                # 空数组是合法结果：全部不值得回复
                if not entries:
                    return []
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    try:
                        idx = int(e.get("idx"))
                    except Exception:
                        continue
                    worth = e.get("worth")
                    if worth is None:
                        continue
                    if isinstance(worth, str):
                        worth = worth.strip().lower() in ("true", "1", "yes", "worth", "值得")
                    else:
                        worth = bool(worth)
                    try:
                        confidence = float(e.get("confidence", 1.0))
                    except Exception:
                        confidence = 1.0
                    reason = e.get("reason")
                    if reason is None:
                        reason = ""
                    else:
                        reason = str(reason).strip()[:50]
                    sender_name = e.get("from")
                    if sender_name is None:
                        sender_name = ""
                    else:
                        sender_name = str(sender_name).strip()[:30]
                    to = e.get("to")
                    if to is None:
                        to = ""
                    else:
                        to = str(to).strip()[:30]
                    directed = e.get("directed")
                    if isinstance(directed, str):
                        directed = directed.strip().lower() in (
                            "true",
                            "1",
                            "yes",
                            "是",
                            "对",
                        )
                    else:
                        directed = bool(directed)
                    results.append(
                        {
                            "idx": idx,
                            "worth": worth,
                            "confidence": confidence,
                            "reason": reason,
                            "from": sender_name,
                            "to": to,
                            "directed": directed,
                        }
                    )
                if results:
                    return results
                # 数组非空但没有任何有效条目：视为格式异常，走正则兜底
        except Exception:
            results = []

        if not results:
            # 正则兜底：输出中的数字视为"值得回复"的序号（如 [1,3]）
            seen: set[int] = set()
            for s in re.findall(r"\d+", text):
                i = int(s)
                if i > 0 and i not in seen:
                    seen.add(i)
                    results.append(
                        {
                            "idx": i,
                            "worth": True,
                            "confidence": 1.0,
                            "reason": "",
                            "from": "",
                            "to": "",
                            "directed": False,
                        }
                    )
        return results or None

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------
    def _build_system_prompt(self) -> str:
        persona = self.bot_persona
        prompt = self.judge_prompt or DEFAULT_JUDGE_PROMPT
        if persona:
            try:
                prompt = prompt.format(persona=persona)
            except (KeyError, IndexError, ValueError):
                prompt = f"{prompt}\n\n机器人人设（简略）：{persona}"
        return prompt + DIRECTED_RULES

    def _build_user_prompt(self, items: list[dict], history_lines: list[str]) -> str:
        parts: list[str] = []
        if history_lines:
            parts.append("最近群聊背景（仅作参考，不属于待判断消息）：")
            parts.extend(f"- {line}" for line in history_lines)
            parts.append("")
        parts.append("待判断消息列表：")
        for i, it in enumerate(items, 1):
            if it.get("reply_to"):
                parts.append(f"{i}. {it['sender_name']} (回复 {it['reply_to']}): {it['text']}")
            else:
                parts.append(f"{i}. {it['sender_name']}: {it['text']}")
        return "\n".join(parts)

    def _build_history_lines(self, umo: str, items: list[dict]) -> list[str]:
        if self.context_window <= 0:
            return []
        skip = {it["uuid"] for it in items}
        lines = [
            rec["line"]
            for rec in self._history.get(umo, deque())
            if rec["uuid"] not in skip
        ]
        return lines[-self.context_window :]

    def _history_append(self, event: AstrMessageEvent, text: str, item_uuid: str) -> None:
        h = self._history.setdefault(
            event.unified_msg_origin,
            deque(maxlen=max(1, self.context_window)),
        )
        h.append(
            {
                "uuid": item_uuid,
                "line": f"{self._sender_name(event)}: {text}",
                "ts": time.time(),
            }
        )

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _cache_get(self, key: tuple) -> Optional[dict]:
        if not self.enable_cache:
            return None
        item = self._cache.get(key)
        if not item:
            return None
        if time.time() - item["ts"] > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return item

    # ---------------- 与「虚拟世界」插件联动 ----------------

    def _virtual_world_plugin(self):
        """在同一个 AstrBot 进程里找到虚拟世界插件的实例。

        找不到（没装 / 加载失败）时返回 None，调用方回落到固定阈值。
        """

        try:
            from astrbot.core.star.star import star_map
        except Exception:
            return None
        for meta in star_map.values():
            if getattr(meta, "name", "") == "astrbot_plugin_virtual_world":
                return getattr(meta, "star_cls", None)
        return None

    async def _effective_threshold(self, umo: Optional[str]) -> float:
        """放行阈值：装了虚拟世界插件时跟随她的孤独感动态变化。

        孤独感越高 → 阈值越低 → 更容易放行（她更想找人说话）。
        会话没在白名单里、或读不到状态时，回落到配置里的固定阈值。
        """

        base = self.confidence_threshold
        if not self.link_virtual_world or not umo:
            return base

        now = time.time()
        cached = self._vw_threshold_cache.get(umo)
        if cached and now - cached[0] < self.vw_cache_seconds:
            return cached[1]

        threshold = base
        plugin = self._virtual_world_plugin()
        if plugin is not None and hasattr(plugin, "social_snapshot"):
            snapshot = None
            try:
                snapshot = await plugin.social_snapshot(umo)
            except Exception as exc:
                logger.debug(f"intent_router: 读取虚拟世界状态失败：{exc}")
            if snapshot:
                if snapshot.get("sleeping"):
                    # 她在睡觉：不做任何放行判断（硬门）。阈值设成 1 以上，
                    # 任何置信度都不可能通过——比"意愿为 0 时的最高阈值"更彻底。
                    if not getattr(self, "_vw_sleep_logged", False):
                        self._vw_sleep_logged = True
                        logger.info(
                            "intent_router: 检测到她在「虚拟世界」里睡觉，"
                            "睡着期间不做放行判断"
                        )
                    self._vw_threshold_cache[umo] = (now, 1.01)
                    return 1.01
                # 优先用虚拟世界算好的「综合回复意愿」；拿不到就退回单看孤独感，
                # 再拿不到就退回更老的 social 字段，保证版本不同步时也能工作。
                raw = snapshot.get("willingness")
                if raw is None:
                    raw = snapshot.get("loneliness")
                if raw is None:
                    raw = snapshot.get("affect", snapshot.get("social"))
                social = max(0.0, min(1.0, float(raw or 0.0)))
                low = min(self.vw_threshold_min, self.vw_threshold_max)
                high = max(self.vw_threshold_min, self.vw_threshold_max)
                threshold = high - (high - low) * social
                if not self._vw_link_logged:
                    self._vw_link_logged = True
                    logger.info(
                        "intent_router: 检测到虚拟世界插件，放行阈值跟随她的综合回复意愿"
                        f"（意愿 0 → {high:.2f}，意愿 1 → {low:.2f}）"
                    )
                if self.debug:
                    logger.info(
                        f"intent_router: 联动阈值 umo={umo} "
                        f"willingness={social:.2f} -> {threshold:.2f}（基准 {base:.2f}）"
                    )

        self._vw_threshold_cache[umo] = (now, threshold)
        if len(self._vw_threshold_cache) > 500:
            self._vw_threshold_cache.clear()
        return threshold

    def _cache_set(self, key: tuple, worth: bool, confidence: float) -> None:
        if not self.enable_cache:
            return
        self._cache[key] = {
            "worth": bool(worth),
            "confidence": float(confidence),
            "ts": time.time(),
        }
        self._prune_cache()

    def _prune_cache(self) -> None:
        if len(self._cache) <= self.cache_max_entries:
            return
        now = time.time()
        expired = [k for k, v in self._cache.items() if now - v["ts"] > self.cache_ttl]
        for k in expired:
            self._cache.pop(k, None)
        if len(self._cache) > self.cache_max_entries:
            # 仍超限：清掉最旧的一半
            ordered = sorted(self._cache.items(), key=lambda kv: kv[1]["ts"])
            for k, _ in ordered[: len(ordered) // 2]:
                self._cache.pop(k, None)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_text(s: str) -> str:
        if not isinstance(s, str):
            return ""
        s = re.sub(r"\[At:[^\]]+\]", "", s)
        s = re.sub(r"<at[^>]*>.*?</at>", "", s, flags=re.I | re.S)
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        name = event.get_sender_name() or ""
        if name.strip():
            return name.strip()
        return str(event.get_sender_id() or "未知用户")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        self._flush_task = asyncio.create_task(self._flush_loop())
        if self.link_virtual_world:
            world = self._virtual_world_plugin()
            if world is not None and hasattr(world, "social_snapshot"):
                logger.info(
                    "intent_router: 检测到「虚拟世界」插件，放行阈值将跟随她的社交欲"
                    f"（社交欲 0 → {max(self.vw_threshold_min, self.vw_threshold_max):.2f}，"
                    f"社交欲 1 → {min(self.vw_threshold_min, self.vw_threshold_max):.2f}）"
                )
            else:
                logger.info(
                    "intent_router: 已开启「虚拟世界」联动，但启动时还没看到它"
                    "（插件加载顺序可能晚于本插件），运行中会自动重试；"
                    "确实没装时沿用固定阈值"
                )
        logger.info(
            "intent_router 已初始化：batch=%s(%d条/%ds) judge_provider=%s 人设=%s",
            "开" if self.enable_batch else "关",
            self.batch_size,
            self.batch_interval,
            self.judge_provider_id or "(会话默认)",
            self.bot_persona,
        )

    async def terminate(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await asyncio.wait_for(self._flush_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._flush_task = None
        logger.info(f"intent_router 已卸载，统计: {self._stats}")
