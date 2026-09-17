"""配置：AstrBot 插件配置（装机时定）+ 看板可调参数（运行时热改）。

看板里改的那部分存成 ``overrides.json``，覆盖内置默认值；改完立即生效，
不用回 AstrBot 面板重载插件。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass
class Tunable:
    """看板里能改的核心参数（都是 0~1 量纲或秒/次数）。"""

    # 概率与门槛
    base_p: float = 0.02
    hard_cap: float = 0.08
    rare_threshold: float = 0.85
    confidence_threshold: float = 0.90
    willingness_floor: float = 0.20
    reply_willingness_line: float = 0.10
    # 概率公式里的系数
    penalty_base: float = 0.5
    load_penalty_floor: float = 0.20
    load_penalty_slope: float = 2.0
    silence_bonus_cap: float = 0.10
    # 时间衰减（半衰期，分钟）
    half_life_short: float = 5.0
    half_life_mid: float = 30.0
    half_life_long: float = 360.0
    mix_short: float = 0.50
    mix_mid: float = 0.30
    mix_long: float = 0.20
    # 硬熔断（次数 / 窗口秒）
    breaker_short_count: int = 2
    breaker_short_window: float = 600.0
    breaker_mid_count: int = 5
    breaker_mid_window: float = 3600.0
    breaker_long_count: int = 12
    breaker_long_window: float = 86400.0
    # 正常回复路径
    reply_cooldown_seconds: float = 60.0
    reply_queue_delay: float = 30.0
    # 路径开关
    proactive_enabled: bool = True
    # 意愿来源
    willingness_default: float = 0.55
    vm_timeout: float = 0.5
    vm_breaker_threshold: int = 5
    vm_breaker_cooldown: float = 60.0
    # 记录保留
    keep_days: int = 30

    def normalized(self) -> "Tunable":
        """夹到合法区间（看板上的输入、旧文件里的值都靠这里兜底）。"""

        return Tunable(
            base_p=_clamp(self.base_p, 0.0, 1.0, 0.02),
            hard_cap=_clamp(self.hard_cap, 0.0, 1.0, 0.08),
            rare_threshold=_clamp(self.rare_threshold, 0.0, 1.0, 0.85),
            confidence_threshold=_clamp(self.confidence_threshold, 0.0, 1.0, 0.90),
            willingness_floor=_clamp(self.willingness_floor, 0.0, 1.0, 0.20),
            reply_willingness_line=_clamp(self.reply_willingness_line, 0.0, 1.0, 0.10),
            penalty_base=_clamp(self.penalty_base, 0.01, 1.0, 0.5),
            load_penalty_floor=_clamp(self.load_penalty_floor, 0.0, 1.0, 0.20),
            load_penalty_slope=_clamp(self.load_penalty_slope, 0.0, 20.0, 2.0),
            silence_bonus_cap=_clamp(self.silence_bonus_cap, 0.0, 1.0, 0.10),
            half_life_short=_clamp(self.half_life_short, 0.5, 1440.0, 5.0),
            half_life_mid=_clamp(self.half_life_mid, 0.5, 2880.0, 30.0),
            half_life_long=_clamp(self.half_life_long, 1.0, 20160.0, 360.0),
            mix_short=_clamp(self.mix_short, 0.0, 1.0, 0.5),
            mix_mid=_clamp(self.mix_mid, 0.0, 1.0, 0.3),
            mix_long=_clamp(self.mix_long, 0.0, 1.0, 0.2),
            breaker_short_count=_clamp_int(self.breaker_short_count, 0, 100, 2),
            breaker_short_window=_clamp(self.breaker_short_window, 10.0, 86400.0, 600.0),
            breaker_mid_count=_clamp_int(self.breaker_mid_count, 0, 200, 5),
            breaker_mid_window=_clamp(self.breaker_mid_window, 10.0, 86400.0, 3600.0),
            breaker_long_count=_clamp_int(self.breaker_long_count, 0, 500, 12),
            breaker_long_window=_clamp(self.breaker_long_window, 60.0, 604800.0, 86400.0),
            reply_cooldown_seconds=_clamp(self.reply_cooldown_seconds, 0.0, 3600.0, 60.0),
            reply_queue_delay=_clamp(self.reply_queue_delay, 0.0, 3600.0, 30.0),
            proactive_enabled=bool(self.proactive_enabled),
            willingness_default=_clamp(self.willingness_default, 0.0, 1.0, 0.55),
            vm_timeout=_clamp(self.vm_timeout, 0.05, 10.0, 0.5),
            vm_breaker_threshold=_clamp_int(self.vm_breaker_threshold, 1, 100, 5),
            vm_breaker_cooldown=_clamp(self.vm_breaker_cooldown, 1.0, 3600.0, 60.0),
            keep_days=_clamp_int(self.keep_days, 1, 365, 30),
        )


@dataclass
class Settings:
    """来自 AstrBot 插件配置的设置（不在看板里改）。"""

    enable: bool = True
    judge_provider_id: str = ""
    bot_name: str = ""
    bot_persona: str = ""
    group_rules: str = ""
    aliases: list[str] = None  # type: ignore[assignment]
    whitelist: list[str] = None  # type: ignore[assignment]
    blacklist: list[str] = None  # type: ignore[assignment]
    pass_prefixes: list[str] = None  # type: ignore[assignment]
    noise_words: list[str] = None  # type: ignore[assignment]
    min_length: int = 2
    context_len: int = 10
    enable_batch: bool = True
    batch_size: int = 5
    batch_interval: float = 30.0
    buffer_seconds: float = 120.0
    release_policy: str = "latest"
    failure_policy: str = "block"
    enable_cache: bool = True
    cache_ttl: float = 300.0
    cache_cap: int = 2000
    json_mode: bool = True
    temperature: float = 0.1
    max_tokens: int = 120
    concurrency: int = 4
    debug: bool = False
    passthrough: bool = False
    web_enabled: bool = True
    data_dir: str = ""

    def __post_init__(self) -> None:
        self.aliases = list(self.aliases or [])
        self.whitelist = list(self.whitelist or [])
        self.blacklist = list(self.blacklist or [])
        self.pass_prefixes = list(self.pass_prefixes or [])
        if self.noise_words is None:
            self.noise_words = ["打卡", "签到", "冒泡", "收到", "顶", "+1"]
        self.release_policy = (
            self.release_policy if self.release_policy in ("latest", "all") else "latest"
        )
        self.failure_policy = (
            self.failure_policy if self.failure_policy in ("block", "pass") else "block"
        )


def cfg_bool(config: Any, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "开启", "是")
    return bool(value)


def cfg_str(config: Any, key: str, default: str) -> str:
    value = config.get(key, default)
    return str(value) if value is not None else default


def cfg_num(config: Any, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def cfg_int(config: Any, key: str, default: int) -> int:
    try:
        return int(float(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def cfg_list(config: Any, key: str, default: list | None = None) -> list:
    value = config.get(key, default if default is not None else [])
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return list(value) if isinstance(value, (list, tuple)) else []


def settings_from_config(config: Any, *, data_dir: str = "") -> Settings:
    """把 AstrBot 的插件配置读成 :class:`Settings`。"""

    return Settings(
        enable=cfg_bool(config, "enable", True),
        judge_provider_id=cfg_str(config, "judge_provider", "").strip(),
        bot_name=cfg_str(config, "bot_name", "").strip(),
        bot_persona=cfg_str(config, "persona", "").strip(),
        group_rules=cfg_str(config, "group_rules", "").strip(),
        aliases=cfg_list(config, "bot_aliases"),
        whitelist=cfg_list(config, "whitelist"),
        blacklist=cfg_list(config, "blacklist"),
        pass_prefixes=cfg_list(config, "pass_prefix"),
        noise_words=cfg_list(config, "noise_words", ["打卡", "签到", "冒泡", "收到", "顶", "+1"]),
        min_length=max(0, cfg_int(config, "min_length", 2)),
        context_len=max(0, cfg_int(config, "context_len", 10)),
        enable_batch=cfg_bool(config, "enable_batch", True),
        batch_size=max(1, cfg_int(config, "batch_size", 5)),
        batch_interval=max(1.0, cfg_num(config, "batch_interval", 30)),
        buffer_seconds=max(10.0, cfg_num(config, "buffer_age", 120)),
        release_policy=cfg_str(config, "release", "latest").strip().lower(),
        failure_policy=cfg_str(config, "fail_mode", "block").strip().lower(),
        enable_cache=cfg_bool(config, "cache", True),
        cache_ttl=max(1.0, cfg_num(config, "cache_ttl", 300)),
        cache_cap=max(10, cfg_int(config, "cache_cap", 2000)),
        json_mode=cfg_bool(config, "json_mode", True),
        temperature=cfg_num(config, "temperature", 0.1),
        max_tokens=max(16, cfg_int(config, "max_tokens", 120)),
        concurrency=max(1, cfg_int(config, "concurrency", 4)),
        debug=cfg_bool(config, "debug", False),
        passthrough=cfg_bool(config, "passthrough", False),
        web_enabled=cfg_bool(config, "web_enabled", True),
        data_dir=data_dir,
    )


class OverrideStore:
    """看板改过的参数：``overrides.json``。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Tunable:
        base = Tunable()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return base
        if not isinstance(raw, dict):
            return base
        known = {item.name for item in fields(Tunable)}
        return Tunable(**{k: v for k, v in raw.items() if k in known}).normalized()

    def save(self, tunable: Tunable) -> Tunable:
        value = tunable.normalized()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(value), ensure_ascii=False, indent=2), "utf-8"
        )
        return value

    def reset(self) -> Tunable:
        try:
            self.path.unlink()
        except OSError:
            pass
        return Tunable()


class RuntimeStore:
    """看板上改的运行时开关（例如总开关），重启后仍然生效。"""

    def __init__(self, path: Path, defaults: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.defaults = dict(defaults or {})

    def load(self) -> dict[str, Any]:
        data = dict(self.defaults)
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return data
        if isinstance(raw, dict):
            data.update(raw)
        return data

    def save(self, data: dict[str, Any]) -> None:
        merged = self.load()
        merged.update({k: v for k, v in (data or {}).items() if k in self.defaults})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")

