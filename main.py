"""智能意图路由（v2）：判断群里的消息值不值得让主人格开口。

分三层，各管各的：

- **路由模型**（`core/judge.py`）只判内容，输出 0~1 结构化分数，不生成回复；
- **本插件**（这个文件）负责概率、熔断、排队、放行/拦截、记录与统计；
- **虚拟世界 VM**（可选）只提供一个「她现在想不想说话」的意愿值。

她说过的话由**发送层钩子**统一记录（不区分来源——其他插件不会配合我们，
所以任何发送动作都算她开口了），密度惩罚、硬熔断都基于这份记录。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.star import Context, Star, register

try:  # 看板用的 Web API 工具；老版本 AstrBot / 单测环境里可能没有
    from astrbot.api.web import error_response, json_response, request
except Exception:  # pragma: no cover - 只在缺少 web 模块时走到
    request = None  # type: ignore[assignment]

    def json_response(data: Any = None, status_code: int = 200, headers: Any = None) -> Any:
        return {"status": "ok", "data": data, "status_code": status_code}

    def error_response(message: str, status_code: int = 400, data: Any = None, headers: Any = None) -> Any:
        return {"status": "error", "message": message, "status_code": status_code}

from .core.config import (
    OverrideStore,
    RuntimeStore,
    Tunable,
    cfg_bool,
    cfg_int,
    cfg_list,
    cfg_str,
    settings_from_config,
)
from .core.decision import (
    BLOCKED,
    IGNORE,
    PROACTIVE,
    REPLY,
    SAFETY,
    Verdict,
    decide_proactive,
    decide_reply,
)
from .core.judge import JudgeCache, JudgePrompt, cache_key, parse_judge_output
from .core.provider import MODE_AUTO, WillingnessProvider
from .core.registry import Registry
from .core.safety import SafetyLayer
from .core.stats import build_report
from .core.storage import Storage
from .core.textutil import clean_text, clip, is_noise

PLUGIN_NAME = "astrbot_plugin_intent_router"
# 比默认 0 高：先于普通插件看到消息，才能决定要不要拦下来
HANDLER_PRIORITY = 100
# 比 VM 的 -100 高，这样"主动插嘴"的约束会追加在它那段世界认知之后
LLM_HOOK_PRIORITY = 90
REINJECT_EXTRA = "intent_router_reinject"
PROACTIVE_EXTRA = "intent_router_proactive"
DATA_DIR_ENV = "INTENT_ROUTER_DATA_DIR"
# 合并放行时，最多把同一批里更早的几句折成背景带上（最新的那几条）
BURST_LEAD_LINES = 6

PROACTIVE_HINT = (
    "\n\n【这一条是偶尔插一句】\n"
    "只偶尔插一句，不展开、不追问、不@人。\n"
    "不超过20字，符合角色口吻。\n"
    "接得住梗就接，接不住就短吐槽。不刷存在感。"
)


def _resolve_data_dir(plugin_dir: str) -> str:
    """数据目录：AstrBot 的 data/plugin_data/<插件名>/（可用环境变量覆盖）。"""

    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        return override
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:
        return os.path.join(plugin_dir, "data")


@register(
    PLUGIN_NAME,
    "exneverbur",
    "智能意图路由：判断群里的消息值不值得让主人格开口回复",
    "v2.1",
)
class IntentRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self.data_dir = _resolve_data_dir(os.path.dirname(os.path.abspath(__file__)))
        self.settings = settings_from_config(config, data_dir=self.data_dir)
        self.overrides = OverrideStore(Path(self.data_dir) / "overrides.json")
        self.tunable: Tunable = self.overrides.load()
        self.runtime = RuntimeStore(
            Path(self.data_dir) / "runtime.json",
            {"enable": bool(self.settings.enable)},
        )
        self._runtime = self.runtime.load()

        self.storage = Storage(Path(self.data_dir) / "router.db")
        self.registry = Registry(self.storage, self.tunable)
        self.provider = WillingnessProvider(
            mode=MODE_AUTO,
            tunable=self.tunable,
            vm_getter=lambda: self._virtual_world_plugin(),
        )
        self.safety = SafetyLayer(
            block_words=cfg_list(config, "block_words"),
            flag_words=cfg_list(config, "flag_words"),
            block_regex=cfg_list(config, "block_regex"),
            max_length=max(200, cfg_int(config, "max_text_length", 2000)),
        )
        self.judge_prompt = JudgePrompt(self.settings)
        self.cache = JudgeCache(
            ttl=self.settings.cache_ttl, capacity=self.settings.cache_cap
        )
        self.enable = bool(self._runtime.get("enable", self.settings.enable))
        self.debug = bool(self.settings.debug)

        # ---------------- 运行时状态 ----------------
        self._buffers: dict[str, deque] = {}
        self._buffer_lock = asyncio.Lock()
        self._history: dict[str, deque] = {}
        self._deadlines: dict[str, float] = {}
        # 每个会话上一次「真的开口」的时刻（含排队延迟），用来算最小间隔
        self._last_release: dict[str, float] = {}
        self._judge_semaphore = asyncio.Semaphore(self.settings.concurrency)
        self._flush_task: Optional[asyncio.Task] = None
        self._delayed: set[asyncio.Task] = set()
        self._seq = 0
        self._vm_logged = False
        self.counters: dict[str, int] = {
            "noise": 0,
            "cache_hits": 0,
            "judged": 0,
            "judge_calls": 0,
            "released": 0,
            "delayed": 0,
            "merged": 0,
            "blocked": 0,
            "proactive": 0,
            "safety_blocked": 0,
            "safety_flagged": 0,
            "judge_failed": 0,
            "dropped_stale": 0,
            "dropped_overflow": 0,
            "reinject_failed": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self._register_web_apis()
        logger.info(
            "intent_router v2 已加载：批量=%s(%d条/%.0fs) 判断模型=%s 主动插嘴=%s",
            "开" if self.settings.enable_batch else "关",
            self.settings.batch_size,
            self.settings.batch_interval,
            self.settings.judge_provider_id or "(会话默认)",
            "开" if self.tunable.proactive_enabled else "关",
        )

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        try:
            self.storage.prune(self.tunable.keep_days)
        except Exception as exc:
            logger.debug(f"intent_router: 清理历史数据失败：{exc}")
        vm = self._virtual_world_plugin()
        if vm is not None:
            logger.info("intent_router: 检测到「虚拟世界」，意愿值跟随她在那个世界里的状态")
            self._vm_logged = True
        else:
            logger.info(
                "intent_router: 没检测到「虚拟世界」，意愿值用固定 %.2f"
                "（运行中会一直重试，装上就自动接上）",
                self.tunable.willingness_default,
            )

    async def terminate(self) -> None:
        task, self._flush_task = self._flush_task, None
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        for pending in list(self._delayed):
            pending.cancel()
        self._delayed.clear()
        try:
            self.storage.close()
        except Exception:
            pass
        logger.info("intent_router 已卸载")

    # ==================================================================
    # 钩子
    # ==================================================================

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=HANDLER_PRIORITY
    )
    async def on_message(self, event: AstrMessageEvent) -> None:
        try:
            await self._route(event)
        except Exception as exc:  # 出问题也必须放行，别把人堵在门口
            logger.error(f"intent_router: 处理消息异常：{exc}", exc_info=True)

    @filter.after_message_sent()
    async def on_bot_message_sent(self, event: AstrMessageEvent) -> None:
        """发送层钩子：只要有发送动作就算她开口了。

        不去识别是哪个插件发的、也不判断发送成没成功——其他插件不会配合我们，
        而"她说了话"这件事本身就是密度惩罚最需要的信号。
        """

        try:
            self.registry.record_speech(event.unified_msg_origin)
        except Exception as exc:
            logger.debug(f"intent_router: 记录发言失败：{exc}")

    @filter.on_llm_request(priority=LLM_HOOK_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """主动插嘴时给主人格加一句约束（只有插嘴才加，正常回复不动）。"""

        if not event.get_extra(PROACTIVE_EXTRA):
            return
        try:
            req.system_prompt = (getattr(req, "system_prompt", "") or "") + PROACTIVE_HINT
        except Exception as exc:
            logger.debug(f"intent_router: 注入插嘴约束失败：{exc}")

    @filter.command("router", alias={"意图路由", "ir"})
    async def cmd_router(self, event: AstrMessageEvent):
        args = (event.get_message_str() or "").strip().split()
        sub = args[1].lower() if len(args) > 1 else "stats"
        umo = event.unified_msg_origin
        if sub in ("stats", "状态"):
            report = build_report(
                storage=self.storage,
                provider_status=self.provider.status(),
                counters=self.counters,
                tunable=self.tunable,
                umo=umo,
                window_seconds=86400,
                recent_limit=5,
            )
            counts = report["counts"]["by_decision"]
            yield event.plain_result(
                "意图路由（近 24 小时）：\n"
                f"· 判断 {report['counts']['judged']} 条，放行 {counts.get(REPLY, 0)}、"
                f"插嘴 {counts.get(PROACTIVE, 0)}、熔断 {counts.get(BLOCKED, 0)}、"
                f"不回 {counts.get(IGNORE, 0)}\n"
                f"· 意愿来源 {report['willingness_provider']['mode']}"
                f"（熔断中：{'是' if report['willingness_provider']['breaker_open'] else '否'}）\n"
                f"· 最近：W={report['latest']['weighted_recent_W']} "
                f"load={report['latest']['normalized_load']} "
                f"willingness={report['latest']['willingness']}"
            )
            return
        if sub in ("on", "off", "开", "关"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以开关意图路由。")
                return
            self.enable = sub in ("on", "开")
            yield event.plain_result("意图路由已" + ("开启" if self.enable else "关闭"))
            return
        if sub in ("good", "bad", "对", "错"):
            if len(args) < 3:
                yield event.plain_result("用法：/router good|bad <判定 id>")
                return
            value = "good" if sub in ("good", "对") else "bad"
            ok = self.storage.set_feedback(int(args[2]), value)
            yield event.plain_result("已记下这条判定" if ok else "没找到这条判定")
            return
        yield event.plain_result(
            "意图路由指令：\n"
            "· /router stats        看当前群的统计\n"
            "· /router on|off       开关（管理员）\n"
            "· /router good|bad <id> 标记这条判断对不对（看板里也能标）"
        )

    # ==================================================================
    # 路由主流程
    # ==================================================================

    async def _route(self, event: AstrMessageEvent) -> None:
        if not self.enable:
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        if event.get_extra(REINJECT_EXTRA, False):
            return  # 本插件重注入的消息，放行
        text = clean_text(event.message_str or "")
        umo = event.unified_msg_origin

        if text:
            self._history_append(umo, text)

        # 直接指向她 / 命令：规则触发，不过模型
        if self._is_direct(event):
            return
        if not self._in_scope(event):
            return
        if not text:
            return  # 纯图片等没有文本的消息不判断

        safety = self.safety.check(text)
        if safety.blocked:
            self.counters["safety_blocked"] += 1
            logger.info("intent_router: 安全层拦下一条消息（%s）", safety.reason)
            self._block_event(event)
            return
        if safety.flagged:
            self.counters["safety_flagged"] += 1

        if is_noise(text, min_length=self.settings.min_length, noise_words=self.settings.noise_words):
            self.counters["noise"] += 1
            self._block_event(event)
            return

        key = cache_key(umo, text)
        now = time.time()
        cached = self.cache.get(key, now)
        if cached is not None:
            self.counters["cache_hits"] += 1
            await self._apply_decision(
                event=event,
                umo=umo,
                text=text,
                verdict=cached,
                cached=True,
                deferred=False,
            )
            return

        item = self._make_item(event, text, key)
        if self.settings.enable_batch:
            self._buffer_add(umo, item)
            self._block_event(event)
            return
        await self._judge_and_decide([item], umo, deferred=False)

    def _is_direct(self, event: AstrMessageEvent) -> bool:
        """直接指向她（@、回复、唤醒前缀、命令、叫名字）→ 直接放行，不花判断的钱。"""

        if getattr(event, "is_at_or_wake_command", False):
            return True
        if event.get_extra("handlers_parsed_params", {}):
            return True
        text = event.message_str or ""
        if text.startswith(("/", "!", "！")):
            return True  # 指令一律放行（含 /router 自己）
        for prefix in self.settings.pass_prefixes:
            if prefix and text.startswith(prefix):
                return True
        lowered = text.lower()
        return any(alias and alias.lower() in lowered for alias in self.settings.aliases)

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        if self.settings.whitelist and gid not in self.settings.whitelist:
            return False
        return not (gid and gid in self.settings.blacklist)

    def _block_event(self, event: AstrMessageEvent) -> None:
        try:
            event.stop_event()
        except Exception:
            pass

    def _mark_wake(self, event: AstrMessageEvent) -> None:
        try:
            event.is_wake = True
        except Exception:
            pass

    # ==================================================================
    # 缓冲：攒一批再判，省 token
    # ==================================================================

    def _make_item(self, event: AstrMessageEvent, text: str, key: str) -> dict[str, Any]:
        self._seq += 1
        return {
            "event": event,
            "text": text,
            "key": key,
            "uuid": uuid.uuid4().hex,
            "ts": time.time(),
            "seq": self._seq,
            "sender": self._sender_name(event),
            "label": self._item_label(event),
        }

    def _item_label(self, event: AstrMessageEvent) -> str:
        """给判断模型看的说话人标注：昵称 + 引用了谁。"""

        who = self._sender_name(event)
        quoted = self._quoted_name(event)
        return f"{who}（回复 {quoted}）" if quoted else who

    def _buffer_add(self, umo: str, item: dict[str, Any]) -> None:
        buf = self._buffers.setdefault(umo, deque())
        was_empty = not buf
        buf.append(item)
        if was_empty:
            self._deadlines[umo] = item["ts"] + self._first_wait()
        elif self.settings.adaptive:
            # 群里还在说就往后挪一点，最多挪到首条入队 + batch_interval
            grew = self._deadlines.get(
                umo, buf[0]["ts"] + float(self.settings.interval_min)
            ) + random.uniform(0.0, max(0.0, float(self.settings.interval_step)))
            self._deadlines[umo] = min(
                grew, buf[0]["ts"] + float(self.settings.batch_interval)
            )
        while len(buf) > 100:
            buf.popleft()
            self.counters["dropped_overflow"] += 1

    def _first_wait(self) -> float:
        """首条消息进队后等多久：自适应时 interval_min ~ 2×interval_min，固定时整个 batch_interval。

        自适应让冷群（只有一两条）等得短、火热群随消息往后挪，最多挪到 batch_interval。
        """

        span = max(0.0, float(self.settings.batch_interval))
        if not self.settings.adaptive:
            return span
        low = min(max(0.0, float(self.settings.interval_min)), span)
        high = min(max(low, low * 2.0), span)
        return random.uniform(low, high)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                now = time.time()
                due: list[str] = []
                for umo, buf in list(self._buffers.items()):
                    if not buf:
                        continue
                    if len(buf) >= self.settings.batch_size:
                        due.append(umo)
                    elif now - buf[0]["ts"] >= self.settings.batch_interval:
                        due.append(umo)
                    elif now >= self._deadlines.get(umo, 0.0):
                        due.append(umo)
                for umo in due:
                    try:
                        await self._flush_group(umo)
                    except Exception as exc:
                        logger.error(f"intent_router: 刷新缓冲失败 umo={umo}：{exc}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"intent_router: 后台刷新循环异常：{exc}")

    async def _flush_group(self, umo: str) -> None:
        items: list[dict[str, Any]] = []
        async with self._buffer_lock:
            buf = self._buffers.get(umo)
            if not buf:
                return
            now = time.time()
            while buf and now - buf[0]["ts"] > self.settings.buffer_seconds:
                buf.popleft()
                self.counters["dropped_stale"] += 1
            while buf and len(items) < 20:
                items.append(buf.popleft())
            if not buf:
                self._buffers.pop(umo, None)
                self._deadlines.pop(umo, None)
        if items:
            await self._judge_and_decide(items, umo)

    # ==================================================================
    # 判断 + 决策 + 落库
    # ==================================================================

    async def _judge_and_decide(
        self, items: list[dict[str, Any]], umo: str, *, deferred: bool = True
    ) -> None:
        """判断一批消息并落地决策。

        ``deferred=True`` 表示这些消息已经被拦下、放在缓冲里了（放行要靠重注入）；
        ``deferred=False`` 是单条即时判断，消息还活着（放行 = 什么都不做）。
        """

        verdicts = await self._judge(items, umo)
        if not verdicts:
            self.counters["judge_failed"] += 1
            if self.settings.failure_policy == "pass":
                if deferred:
                    self._release_batch(
                        [{"item": item, "delay": 0.0, "judgement": 0} for item in items],
                        items,
                        umo,
                    )
            else:
                self.counters["blocked"] += len(items)
                for item in items:
                    self._block_event(item["event"])
            return
        ready: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            verdict = verdicts.get(index)
            if verdict is None:
                # 模型漏了这条：当成不值得回，别去打扰主人格
                self.cache.set(item["key"], Verdict(idx=index, worth=False), time.time())
                self.counters["blocked"] += 1
                self._block_event(item["event"])
                continue
            self.cache.set(item["key"], verdict, time.time())
            release = await self._apply_decision(
                event=item["event"],
                umo=umo,
                text=item["text"],
                verdict=verdict,
                deferred=deferred,
            )
            if release is not None:
                ready.append(
                    {"item": item, "delay": release[0], "judgement": release[1]}
                )
        # 放行统一放在批处理这一层：一批里每条各自放行的话，她一句话就会回一屏
        if deferred and ready:
            self._release_batch(ready, items, umo)

    async def _apply_decision(
        self,
        *,
        event: AstrMessageEvent,
        umo: str,
        text: str,
        verdict: Verdict,
        cached: bool = False,
        deferred: bool = True,
    ) -> Optional[tuple[float, int]]:
        """把一条判断落成"回 / 插一句 / 不回"，并写进流水。

        返回值：``None`` = 不回；``(延迟秒数, 流水 id)`` = 该放行。
        放行本身交给调用方（见 :meth:`_release_batch`）：一批里每条各自放行的话，
        她一句话就会回一屏。
        """

        now = time.time()
        snapshot = self.registry.snapshot(umo, now)
        willing = await self.provider.get(umo)

        if willing.sleeping:
            # 她在睡觉：静默，连记录都不用（睡眠门禁在 VM 那边也有，这里再挡一道）
            self.counters["blocked"] += 1
            self._block_event(event)
            return None

        safety = self.safety.check(text, risk=verdict.risk)
        if safety.blocked:
            self.counters["safety_blocked"] += 1
            self._block_event(event)
            self._persist(
                umo=umo,
                text=text,
                verdict=verdict,
                decision=SAFETY,
                probability=0.0,
                reason=safety.reason,
                willing=willing.value,
                snapshot=snapshot,
            )
            return None

        if verdict.worth:
            outcome = decide_reply(
                verdict,
                willingness=willing.value,
                density=snapshot.as_density(),
                tunable=self.tunable,
            )
            judgement = self._persist(
                umo=umo,
                text=text,
                verdict=verdict,
                decision=outcome.decision,
                probability=outcome.probability,
                reason=outcome.reason,
                willing=willing.value,
                snapshot=snapshot,
            )
            if self.debug:
                logger.info(
                    "intent_router 放行：%s（worth=%s score=%.2f 意愿=%.2f %s）",
                    clip(text, 40),
                    verdict.worth,
                    verdict.reply_score,
                    willing.value,
                    "缓存" if cached else "",
                )
            return (float(outcome.delay or 0.0), judgement)

        outcome = decide_proactive(
            verdict,
            willingness=willing.value,
            density=snapshot.as_density(),
            counts=snapshot.counts,
            tunable=self.tunable,
        )
        judgement = self._persist(
            umo=umo,
            text=text,
            verdict=verdict,
            decision=outcome.decision,
            probability=outcome.probability,
            reason=outcome.reason,
            willing=willing.value,
            snapshot=snapshot,
        )
        if outcome.decision == PROACTIVE and random.random() < outcome.probability:
            self.counters["proactive"] += 1
            try:
                event.set_extra(PROACTIVE_EXTRA, True)
            except Exception:
                pass
            logger.info(
                "intent_router: 主动插一句（p=%.3f）%s",
                outcome.probability,
                clip(text, 40),
            )
            return (0.0, judgement)
        self.counters["blocked"] += 1
        self._block_event(event)
        if self.debug:
            logger.info(
                "intent_router 不回复：%s（rare=%.2f confidence=%.2f → %s）",
                clip(text, 40),
                verdict.rare_interject_score,
                verdict.confidence,
                outcome.reason,
            )
        return None

    def _persist(
        self,
        *,
        umo: str,
        text: str,
        verdict: Verdict,
        decision: str,
        probability: float,
        reason: str,
        willing: float,
        snapshot: Any,
    ) -> int:
        """写一条判定流水，返回它的 id（看板上就是那一行的编号）。"""

        try:
            return self.storage.record_judgement(
                umo=umo,
                ts=time.time(),
                sender=verdict.sender,
                text=text,
                worth=verdict.worth,
                scores=verdict.scores(),
                decision=decision,
                probability=probability,
                breakdown={
                    "willingness": round(float(willing), 4),
                    "weighted_recent": snapshot.weighted_recent,
                    "normalized_load": snapshot.normalized_load,
                    "silence_seconds": snapshot.silence_seconds,
                    "counts": snapshot.counts,
                    "to": verdict.to,
                    "directed": verdict.directed,
                    "suggested_action": verdict.suggested_action,
                },
                reason=reason,
            )
        except Exception as exc:
            logger.debug(f"intent_router: 写判定流水失败：{exc}")
            return 0

    # ==================================================================
    # 放行 / 排队 / 重注入
    # ==================================================================

    def _release_batch(
        self, ready: list[dict[str, Any]], items: list[dict[str, Any]], umo: str
    ) -> None:
        """一批里被判"该回"的那些：决定真正放行几条。

        ``latest``（默认）只放行最新那条，同一批里更早的几句折成一段背景带上——
        让她一次把这波话回完，而不是一条消息回一句。``all`` 保持每条各自放行。
        """

        if not ready:
            return
        ordered = sorted(ready, key=lambda entry: int(entry["item"].get("seq") or 0))
        if self.settings.release_policy == "latest":
            picked = [ordered[-1]]
            for entry in ordered[:-1]:
                self.counters["merged"] += 1
                self._mark_merged(entry.get("judgement"))
            picked[0]["lead"] = self._burst_lead(items, picked[0]["item"])
        else:
            picked = ordered
        for entry in picked:
            self._release_entry(entry, umo)

    def _release_entry(self, entry: dict[str, Any], umo: str) -> None:
        """把一条放回管道：该等就等（意愿排队 + 会话最小间隔），到点再重注入。"""

        delay = max(0.0, float(entry.get("delay") or 0.0))
        cooldown = self._cooldown_wait(umo)
        if cooldown > delay:
            delay = cooldown
        if delay > 0:
            self.counters["delayed"] += 1
            self._schedule_release(
                entry["item"]["event"], delay, lead=str(entry.get("lead") or "")
            )
        else:
            self._release_now(entry["item"]["event"], str(entry.get("lead") or ""))
        # 记的是"真的发出去"的时刻，后面几条都按它算间隔
        self._last_release[umo] = time.time() + delay

    def _cooldown_wait(self, umo: str) -> float:
        """离上次开口还差多久：同一条会话里两次放行至少隔这么久。"""

        last = float(self._last_release.get(umo, 0.0))
        if not last:
            return 0.0
        return max(0.0, last + float(self.tunable.reply_cooldown_seconds) - time.time())

    def _burst_lead(self, items: list[dict[str, Any]], chosen: dict[str, Any]) -> str:
        """同一批里比它更早的那几句，折成一小段背景带上。

        这些消息早被打回了，主人格本来只看得见最后一条；带上背景她才接得上话。
        """

        chosen_seq = int(chosen.get("seq") or 0)
        lines = [
            f"{str(item.get('sender') or '有人')}：{clip(str(item.get('text') or ''), 60)}"
            for item in items
            if int(item.get("seq") or 0) < chosen_seq and str(item.get("text") or "").strip()
        ]
        if not lines:
            return ""
        lines = lines[-BURST_LEAD_LINES:]
        body = "\n".join(f"- {line}" for line in lines)
        return f"[群里连着发的几条]\n{body}\n"

    def _mark_merged(self, judgement: Any) -> None:
        """被合并掉的那几条：在流水里补一句，免得看板上看着像回了好几次。"""

        try:
            judgement_id = int(judgement or 0)
        except (TypeError, ValueError):
            return
        if judgement_id:
            self.storage.mark_merged(judgement_id, "；同一批里被合并成一次回复")

    def _schedule_release(
        self, event: AstrMessageEvent, delay: float, *, lead: str = ""
    ) -> None:
        """排队延迟一会儿再放行（而不是直接拒绝）。"""

        async def later() -> None:
            try:
                await asyncio.sleep(max(0.0, delay))
                self._release_now(event, lead)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"intent_router: 延迟放行失败：{exc}")

        task = asyncio.create_task(later())
        self._delayed.add(task)
        task.add_done_callback(self._delayed.discard)

    def _release_now(self, event: AstrMessageEvent, lead: str = "") -> None:
        self._reinject(event, lead=lead)
        self.counters["released"] += 1

    def _reinject(self, event: AstrMessageEvent, *, lead: str = "") -> None:
        """把消息副本（带 At 唤醒标记）放回事件队列，重新走一遍管道。

        ``lead`` 是这一批里更早的几句（见 :meth:`_burst_lead`），拼在正文前面。
        """

        try:
            new_event = copy.copy(event)
            if hasattr(new_event, "_force_stopped"):
                new_event._force_stopped = False
            if hasattr(new_event, "clear_result"):
                new_event.clear_result()
            if hasattr(new_event, "_has_send_oper"):
                new_event._has_send_oper = False
            new_event._extras = dict(getattr(event, "_extras", {}) or {})
            message_obj = getattr(new_event, "message_obj", None)
            chain = getattr(message_obj, "message", None)
            self_id = event.get_self_id()
            if isinstance(chain, list):
                has_at_self = any(
                    isinstance(item, At) and str(getattr(item, "qq", "")) == str(self_id)
                    for item in chain
                )
                if not has_at_self:
                    chain.insert(0, At(qq=self_id, name=self_id))
            if lead:
                body = str(new_event.message_str or "").strip()
                head = str(lead).strip()
                new_event.message_str = f"{head}\n{body}".strip()
            new_event.set_extra(REINJECT_EXTRA, True)
            self.context.get_event_queue().put_nowait(new_event)
        except Exception as exc:
            self.counters["reinject_failed"] += 1
            logger.error(f"intent_router: 重注入失败：{exc}")

    # ==================================================================
    # 判断模型
    # ==================================================================

    async def _judge(self, items: list[dict[str, Any]], umo: str) -> dict[int, Verdict]:
        """调路由模型，一次判一批。返回 ``{idx: Verdict}``（失败返回空）。"""

        if not items:
            return {}
        history = self._history_lines(umo)
        payload = [
            {"idx": index, "sender": item["sender"], "label": item["label"], "text": item["text"]}
            for index, item in enumerate(items, start=1)
        ]
        system_prompt = self.judge_prompt.system()
        user_prompt = self.judge_prompt.user(payload, history)
        raw, error = await self._call_judge(system_prompt, user_prompt, umo)
        self.counters["judge_calls"] += 1
        self.counters["judged"] += len(items)
        if error or not raw:
            logger.warning(f"intent_router: 判断失败：{error or '没有返回内容'}")
            return {}
        verdicts = parse_judge_output(raw)
        if not verdicts:
            logger.warning(f"intent_router: 判断输出解析失败：{clip(raw, 120)}")
            return {}
        return verdicts

    async def _call_judge(
        self, system_prompt: str, user_prompt: str, umo: str
    ) -> tuple[str, str]:
        """调用判断模型，返回 ``(原始文本, 错误说明)``。"""

        provider = None
        if self.settings.judge_provider_id:
            try:
                provider = self.context.get_provider_by_id(self.settings.judge_provider_id)
            except Exception:
                provider = None
            if provider is None:
                logger.warning(
                    "intent_router: 找不到判断用的 Provider「%s」，改用会话默认",
                    self.settings.judge_provider_id,
                )
        if provider is None:
            try:
                provider = await self.context.get_using_provider_async(umo)
            except Exception as exc:
                return "", f"没有可用的判断模型：{exc}"
        if provider is None:
            return "", "没有可用的判断模型"

        kwargs: dict[str, Any] = {
            "temperature": max(0.0, min(0.3, float(self.settings.temperature))),
            "max_tokens": int(self.settings.max_tokens),
        }
        if self.settings.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        async with self._judge_semaphore:
            try:
                response = await provider.text_chat(
                    prompt=user_prompt, system_prompt=system_prompt, **kwargs
                )
            except Exception as exc:
                logger.debug(f"intent_router: 带参调用失败（{exc}），退回最小参数重试")
                try:
                    response = await provider.text_chat(
                        prompt=user_prompt, system_prompt=system_prompt
                    )
                except Exception as retry_exc:
                    return "", str(retry_exc)
        if response is None:
            return "", "判断模型没有返回内容"
        self._record_usage(response)
        raw = str(getattr(response, "completion_text", "") or "")
        if self.debug:
            logger.info("intent_router 判断输出：\n%s", raw)
        return raw, ""

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        for key, attr in (
            ("prompt_tokens", "input"),
            ("completion_tokens", "output"),
            ("total_tokens", "total"),
        ):
            try:
                self.counters[key] += int(getattr(usage, attr, 0) or 0)
            except Exception:
                continue

    # ==================================================================
    # 小工具
    # ==================================================================

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        name = str(event.get_sender_name() or "").strip()
        return name or str(event.get_sender_id() or "未知用户")

    @staticmethod
    def _quoted_name(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        for component in list(getattr(message_obj, "message", []) or []):
            if isinstance(component, Reply):
                nick = str(getattr(component, "sender_nickname", "") or "").strip()
                return nick or str(getattr(component, "sender_id", "") or "")
        return ""

    def _history_append(self, umo: str, text: str) -> None:
        history = self._history.setdefault(umo, deque(maxlen=40))
        history.append((time.time(), clip(text, 200)))

    def _history_lines(self, umo: str) -> list[str]:
        """最近群聊（给判断模型当局部上下文），按时间正序。"""

        history = list(self._history.get(umo, []))[-max(0, self.settings.context_len) :]
        return [f"{time.strftime('%H:%M', time.localtime(ts))} {text}" for ts, text in history]
    def _virtual_world_plugin(self) -> Any:
        """在同一个 AstrBot 进程里找「虚拟世界」插件实例（没装返回 None）。"""

        try:
            from astrbot.core.star.star import star_map
        except Exception:
            return None
        for meta in star_map.values():
            if getattr(meta, "name", "") == "astrbot_plugin_virtual_world":
                return getattr(meta, "star_cls", None)
        return None

    # ==================================================================
    # 看板用的 Web API
    # ==================================================================

    def _register_web_apis(self) -> None:
        if not self.settings.web_enabled or request is None:
            return
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        register = register_api
        p = PLUGIN_NAME
        register(f"/{p}/status", self.api_status, ["GET"], "运行状态")
        register(f"/{p}/stats", self.api_stats, ["GET"], "统计")
        register(f"/{p}/judgements", self.api_judgements, ["GET"], "判定流水")
        register(f"/{p}/feedback", self.api_feedback, ["POST"], "标记判定好坏")
        register(f"/{p}/params", self.api_params, ["GET", "POST"], "核心参数")
        register(f"/{p}/control", self.api_control, ["POST"], "开关")
        register(f"/{p}/data", self.api_data, ["GET", "POST"], "导出 / 清空数据")

    def _param_dict(self) -> dict[str, Any]:
        return {
            "base_p": self.tunable.base_p,
            "hard_cap": self.tunable.hard_cap,
            "rare_threshold": self.tunable.rare_threshold,
            "confidence_threshold": self.tunable.confidence_threshold,
            "willingness_floor": self.tunable.willingness_floor,
            "reply_willingness_line": self.tunable.reply_willingness_line,
            "penalty_base": self.tunable.penalty_base,
            "load_penalty_floor": self.tunable.load_penalty_floor,
            "load_penalty_slope": self.tunable.load_penalty_slope,
            "silence_bonus_cap": self.tunable.silence_bonus_cap,
            "half_life_short": self.tunable.half_life_short,
            "half_life_mid": self.tunable.half_life_mid,
            "half_life_long": self.tunable.half_life_long,
            "mix_short": self.tunable.mix_short,
            "mix_mid": self.tunable.mix_mid,
            "mix_long": self.tunable.mix_long,
            "breaker_short_count": self.tunable.breaker_short_count,
            "breaker_short_window": self.tunable.breaker_short_window,
            "breaker_mid_count": self.tunable.breaker_mid_count,
            "breaker_mid_window": self.tunable.breaker_mid_window,
            "breaker_long_count": self.tunable.breaker_long_count,
            "breaker_long_window": self.tunable.breaker_long_window,
            "reply_cooldown_seconds": self.tunable.reply_cooldown_seconds,
            "reply_queue_delay": self.tunable.reply_queue_delay,
            "proactive_enabled": self.tunable.proactive_enabled,
            "willingness_default": self.tunable.willingness_default,
            "vm_timeout": self.tunable.vm_timeout,
            "vm_breaker_threshold": self.tunable.vm_breaker_threshold,
            "vm_breaker_cooldown": self.tunable.vm_breaker_cooldown,
            "keep_days": self.tunable.keep_days,
        }

    def _status_dict(self) -> dict[str, Any]:
        try:
            sessions = sorted(self.storage.counts_by_umo(time.time() - 7 * 86400))
        except Exception:
            sessions = []
        return {
            "enable": bool(self.enable),
            "proactive_enabled": bool(self.tunable.proactive_enabled),
            "judge_provider_id": self.settings.judge_provider_id,
            "batch": {
                "enabled": bool(self.settings.enable_batch),
                "size": self.settings.batch_size,
                "interval": self.settings.batch_interval,
                "adaptive": bool(self.settings.adaptive),
                "interval_min": self.settings.interval_min,
                "interval_step": self.settings.interval_step,
                "buffer_seconds": self.settings.buffer_seconds,
                "release_policy": self.settings.release_policy,
            },
            "context_len": self.settings.context_len,
            "groups": {
                "whitelist": list(self.settings.whitelist),
                "blacklist": list(self.settings.blacklist),
            },
            "aliases": list(self.settings.aliases),
            "bot_name": self.settings.bot_name,
            "persona_chars": len(self.settings.bot_persona or ""),
            "vm_linked": self._virtual_world_plugin() is not None,
            "willingness_provider": self.provider.status(),
            "storage": self.storage.stats(),
            "counters": dict(self.counters),
            "cache_hits": self.cache.hits,
            "buffered": {umo: len(buf) for umo, buf in self._buffers.items() if buf},
            "sessions": sessions,
        }

    async def api_status(self):
        return json_response(self._status_dict())

    async def api_stats(self):
        umo = str(request.query.get("session", "") or "")
        try:
            window = float(request.query.get("window", 604800) or 604800)
        except (TypeError, ValueError):
            window = 604800
        try:
            limit = int(request.query.get("limit", 50) or 50)
        except (TypeError, ValueError):
            limit = 50
        report = build_report(
            storage=self.storage,
            provider_status=self.provider.status(),
            counters=self.counters,
            tunable=self.tunable,
            umo=umo,
            window_seconds=window,
            recent_limit=max(1, min(200, limit)),
        )
        return json_response(report)

    async def api_judgements(self):
        umo = str(request.query.get("session", "") or "")
        try:
            limit = int(request.query.get("limit", 50) or 50)
        except (TypeError, ValueError):
            limit = 50
        rows = self.storage.recent_judgements(umo=umo, limit=max(1, min(500, limit)))
        return json_response({"judgements": rows})

    async def api_feedback(self):
        payload = await request.json(default={}) or {}
        try:
            judgement_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少判定 id")
        value = str(payload.get("value") or "")
        if value not in ("good", "bad", ""):
            return error_response("value 只能是 good / bad / 空")
        ok = self.storage.set_feedback(judgement_id, value)
        return json_response({"ok": bool(ok)})

    async def api_params(self):
        if request.method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            payload = await request.json(default={}) or {}
            if payload.get("reset"):
                self.tunable = self.overrides.reset()
            else:
                incoming = payload.get("params")
                if not isinstance(incoming, dict):
                    return error_response("params 必须是对象")
                merged = {**self._param_dict(), **incoming}
                self.tunable = self.overrides.save(Tunable(**merged))
            self.registry.set_tunable(self.tunable)
            self.provider.set_tunable(self.tunable)
            logger.info("intent_router: 看板更新了核心参数")
            return json_response({"ok": True, "params": self._param_dict()})
        return json_response({"params": self._param_dict()})

    async def api_control(self):
        payload = await request.json(default={}) or {}
        changed: list[str] = []
        if "enable" in payload:
            self.enable = bool(payload.get("enable"))
            self.runtime.save({"enable": self.enable})
            changed.append(f"插件已{'开启' if self.enable else '关闭'}")
        if "proactive_enabled" in payload:
            self.tunable = self.overrides.save(
                Tunable(**{**self._param_dict(), "proactive_enabled": bool(payload.get("proactive_enabled"))})
            )
            self.registry.set_tunable(self.tunable)
            self.provider.set_tunable(self.tunable)
            changed.append(
                f"主动插嘴已{'开启' if self.tunable.proactive_enabled else '关闭'}"
            )
        return json_response(
            {"ok": True, "changed": changed, "enable": self.enable, "status": self._status_dict()}
        )

    async def api_data(self):
        if request.method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            payload = await request.json(default={}) or {}
            if payload.get("clear"):
                tables = payload.get("tables") or ["speech", "judgement"]
                self.storage.clear(tuple(str(item) for item in tables))
                self.cache.clear()
                return json_response({"ok": True})
            if payload.get("prune"):
                removed = self.storage.prune(self.tunable.keep_days)
                return json_response({"ok": True, "removed": removed})
            return error_response("没什么可做的")
        umo = str(request.query.get("session", "") or "")
        rows = self.storage.recent_judgements(umo=umo, limit=5000)
        speech = self.storage.speech_since(umo, 0.0) if umo else []
        return json_response(
            {
                "exported_at": time.time(),
                "params": self._param_dict(),
                "judgements": rows,
                "speech_count": len(speech),
            }
        )
