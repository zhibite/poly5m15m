"""Runtime-editable ArbSettings with JSON persistence.

The config page writes only the fields it can safely change while the
process is running. Overrides are stored in
``data/runtime_config.json`` and re-applied by ``arb_settings()`` at the
next start. Fields that need a restart (mode, assets, port, paths,
MarketWS) are exposed read-only.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.infra.logging import get_logger

log = get_logger(__name__)

_DATA_DIR = Path(
    os.getenv("POLYMARKET_DATA_DIR")
    or (Path(__file__).resolve().parent.parent / "data")
)
OVERRIDES_PATH = Path(
    os.getenv("ARB_RUNTIME_CONFIG_PATH") or (_DATA_DIR / "runtime_config.json")
)


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    group: str
    kind: str = "float"          # int | float | bool | select | text
    editable: bool = True
    restart: bool = False
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""


GROUP_ORDER = (
    "风控 / 仓位",
    "手续费 / 边际",
    "配对 / Strike",
    "执行",
    "循环",
    "系统（只读，重启生效）",
)


def _spec(*args, **kwargs) -> FieldSpec:
    return FieldSpec(*args, **kwargs)


FIELD_SPECS: tuple[FieldSpec, ...] = (
    # ── 风控 / 仓位 ─────────────────────────────────────────────────────
    _spec("MAX_SHARES_PER_PAIR", "单对最大份数", "风控 / 仓位", "float", min=0, max=100000, step=1,
          help="单个配对最多买多少份；实际还会受深度和单对成本限制"),
    _spec("MIN_SHARES_PER_PAIR", "单对最小份数", "风控 / 仓位", "float", min=0, max=10000, step=1,
          help="低于这个份数直接不做"),
    _spec("MAX_NOTIONAL_PER_PAIR", "单对最大成本 (USD)", "风控 / 仓位", "float", min=0, max=100000, step=1,
          help="超出预算时缩小下单量，不是跳过机会"),
    _spec("MAX_OPEN_POSITIONS", "最大同时持仓数", "风控 / 仓位", "int", min=1, max=100, step=1),
    _spec("MAX_OPEN_NOTIONAL", "最大在险成本 (USD)", "风控 / 仓位", "float", min=0, max=100000, step=1),
    _spec("MAX_DAILY_LOSS_USD", "当日最大亏损 (USD)", "风控 / 仓位", "float", min=0, max=100000, step=1,
          help="当日已实现亏损达到后停止开新仓"),
    _spec("MAX_CONSECUTIVE_FAILURES", "连续失败上限", "风控 / 仓位", "int", min=1, max=100, step=1),
    _spec("FAILURE_COOLDOWN_S", "失败冷却 (秒)", "风控 / 仓位", "float", min=0, max=3600, step=1),

    # ── 手续费 / 边际 ───────────────────────────────────────────────────
    _spec("FEE_RATE", "Taker 费率", "手续费 / 边际", "float", min=0, max=1, step=0.001,
          help="官方公式 fee = 份数 × rate × p × (1-p)，maker 为 0"),
    _spec("FEE_BUFFER_PCT", "手续费缓冲比例", "手续费 / 边际", "float", min=0, max=1, step=0.01,
          help="在估算手续费上再加的安全边际"),
    _spec("FEE_BUFFER_USD", "手续费缓冲 (USD)", "手续费 / 边际", "float", min=0, max=100, step=0.01),
    _spec("MIN_EDGE_USD", "最小保底利润 (USD)", "手续费 / 边际", "float", min=0, max=100, step=0.01),
    _spec("MIN_EDGE_PCT", "最小边际比例", "手续费 / 边际", "float", min=0, max=1, step=0.001),
    _spec("SAME_MARKET_MIN_EDGE_USD", "同市场最小利润 (USD)", "手续费 / 边际", "float", min=0, max=100, step=0.01),
    _spec("SAME_MARKET_MIN_EDGE_PCT", "同市场最小边际", "手续费 / 边际", "float", min=0, max=1, step=0.001),

    # ── 配对 / Strike ───────────────────────────────────────────────────
    _spec("STRIKE_SOURCE", "Strike 来源", "配对 / Strike", "select",
          choices=("auto", "binance", "description"),
          help="auto=优先解析市场描述，缺失时用 Binance 窗口 open"),
    _spec("MIN_STRIKE_GAP_BPS", "最小 strike 间距 (bps)", "配对 / Strike", "float", min=0, max=1000, step=1,
          help="strike 太近时方向可能翻转；默认 5"),
    _spec("STRIKE_SETTLE_S", "Strike 确认等待 (秒)", "配对 / Strike", "float", min=0, max=60, step=0.5),
    _spec("STOP_OPEN_BEFORE_END_S", "结算前停止开仓 (秒)", "配对 / Strike", "float", min=0, max=300, step=1),
    _spec("MIN_SECONDS_TO_END_S", "最小剩余时间 (秒)", "配对 / Strike", "float", min=0, max=300, step=1),
    _spec("MAX_QUOTE_AGE_S", "最大报价年龄 (秒)", "配对 / Strike", "float", min=0.1, max=60, step=0.1,
          help="ask 超过这个时间未更新就回退 REST 深度"),
    _spec("COOLDOWN_S", "执行冷却 (秒)", "配对 / Strike", "float", min=0, max=3600, step=0.5),

    # ── 执行 ────────────────────────────────────────────────────────────
    _spec("LEG_RISK_POLICY", "腿风险策略", "执行", "select", choices=("unwind", "complete"),
          help="unwind=卖出多余腿；complete=先补失败腿"),
    _spec("MAX_LEG_SLIPPAGE_TICKS", "补腿最大滑点 (tick)", "执行", "int", min=0, max=20, step=1),
    _spec("UNWIND_RETRIES", "Unwind 重试次数", "执行", "int", min=1, max=20, step=1),
    _spec("UNWIND_RETRY_DELAY_S", "Unwind 重试间隔 (秒)", "执行", "float", min=0, max=10, step=0.1),
    _spec("SELL_SLIPPAGE_PCT", "卖出滑点 (%)", "执行", "float", min=0, max=20, step=0.1),
    _spec("ALLOW_CROSS_MARKET", "启用两腿跨市场", "执行", "bool"),
    _spec("ALLOW_SAME_MARKET", "启用同市场组合", "执行", "bool"),
    _spec("ALLOW_COMPLETE_SET", "启用四腿完整组合", "执行", "bool"),
    _spec("REQUIRE_SAME_RESOLUTION_SOURCE", "要求结算源一致", "执行", "bool"),
    _spec("ALLOW_UNKNOWN_RESOLUTION_SOURCE", "允许未知结算源", "执行", "bool",
          help="Live 模式默认关闭；Paper 模式本身允许采集"),

    # ── 循环 ────────────────────────────────────────────────────────────
    _spec("TICK_S", "交易循环间隔 (秒)", "循环", "float", min=0.05, max=5, step=0.05),
    _spec("DISCOVERY_S", "市场发现间隔 (秒)", "循环", "float", min=1, max=60, step=1),
    _spec("MARKET_META_REFRESH_S", "市场元数据刷新 (秒)", "循环", "float", min=5, max=600, step=5),
    _spec("SETTLEMENT_POLL_S", "结算轮询 (秒)", "循环", "float", min=1, max=60, step=1),
    _spec("STATUS_S", "状态写入间隔 (秒)", "循环", "float", min=0.2, max=10, step=0.2),

    # ── 系统（只读） ────────────────────────────────────────────────────
    _spec("ENABLED", "启用 bot", "系统（只读，重启生效）", "bool", editable=False, restart=True),
    _spec("ASSETS", "交易资产", "系统（只读，重启生效）", "text", editable=False, restart=True),
    _spec("PAPER_TRADING", "Paper 模式", "系统（只读，重启生效）", "bool", editable=False, restart=True),
    _spec("LIVE_ENABLED", "Live 开关", "系统（只读，重启生效）", "bool", editable=False, restart=True),
    _spec("WEB_PORT", "Web 端口", "系统（只读，重启生效）", "int", editable=False, restart=True),
    _spec("WEB_SESSION_TTL_S", "登录会话有效期 (秒)", "系统（只读，重启生效）", "int", editable=False, restart=True),
    _spec("DB_PATH", "数据库路径", "系统（只读，重启生效）", "text", editable=False, restart=True),
    _spec("STATUS_PATH", "状态文件路径", "系统（只读，重启生效）", "text", editable=False, restart=True),
    _spec("USE_MARKET_WS", "使用行情 WS", "系统（只读，重启生效）", "bool", editable=False, restart=True),
    _spec("LOG_LEVEL", "日志级别", "系统（只读，重启生效）", "text", editable=False, restart=True),
)

_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in FIELD_SPECS}


def coerce_value(spec: FieldSpec, raw: Any) -> Any:
    """Validate/coerce one form value according to its spec."""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{spec.label} 需要 true/false")

    if spec.kind == "select":
        value = str(raw or "").strip().lower()
        if value not in spec.choices:
            raise ValueError(
                f"{spec.label} 只能是：{', '.join(spec.choices)}"
            )
        return value

    if spec.kind == "text":
        return str(raw or "").strip()

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{spec.label} 需要数字") from exc
    if spec.kind == "int":
        if value != int(value):
            raise ValueError(f"{spec.label} 需要整数")
        value = int(value)
    if spec.min is not None and value < spec.min:
        raise ValueError(f"{spec.label} 不能小于 {spec.min:g}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"{spec.label} 不能大于 {spec.max:g}")
    return value


def load_overrides() -> dict[str, Any]:
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning("runtime_config.read_failed", path=str(OVERRIDES_PATH), error=str(exc))
        return {}
    if not isinstance(raw, dict):
        log.warning("runtime_config.not_an_object", path=str(OVERRIDES_PATH))
        return {}
    return raw


def save_overrides(values: dict[str, Any]) -> None:
    current = load_overrides()
    current.update(values)
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(current, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(
        dir=str(OVERRIDES_PATH.parent), prefix=".runtime-config.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, OVERRIDES_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_overrides(cfg: Any) -> list[str]:
    """Apply persisted editable overrides to a freshly built ArbSettings."""
    applied: list[str] = []
    for key, raw in load_overrides().items():
        spec = _BY_KEY.get(key)
        if spec is None or not spec.editable:
            continue
        try:
            value = coerce_value(spec, raw)
        except ValueError as exc:
            log.warning("runtime_config.invalid_override", key=key, error=str(exc))
            continue
        setattr(cfg, key, value)
        applied.append(key)
    if applied:
        log.info("runtime_config.applied", keys=applied, path=str(OVERRIDES_PATH))
    return applied


def update_config(
    cfg: Any, values: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Validate all values, persist them, then apply to the live cfg."""
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")
    cleaned: dict[str, Any] = {}
    restart: list[str] = []
    for key, raw in values.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"未知配置项：{key}")
        if not spec.editable:
            raise ValueError(f"{spec.label} 是只读项，需要修改环境变量并重启")
        cleaned[key] = coerce_value(spec, raw)
        if spec.restart:
            restart.append(key)

    if cleaned:
        save_overrides(cleaned)
        for key, value in cleaned.items():
            setattr(cfg, key, value)
        log.info("runtime_config.updated", keys=sorted(cleaned))
    return sorted(cleaned), restart


def config_payload(cfg: Any) -> dict:
    fields = []
    for spec in FIELD_SPECS:
        fields.append(
            {
                "key": spec.key,
                "label": spec.label,
                "group": spec.group,
                "kind": spec.kind,
                "editable": spec.editable,
                "restart": spec.restart,
                "min": spec.min,
                "max": spec.max,
                "step": spec.step,
                "choices": list(spec.choices),
                "help": spec.help,
                "value": getattr(cfg, spec.key, None),
            }
        )
    return {
        "mode": "live" if getattr(cfg, "live_trading", False) else "paper",
        "groups": list(GROUP_ORDER),
        "fields": fields,
    }
