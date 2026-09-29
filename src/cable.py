"""斜拉索索力缓变识别（温度基线）纯规则模块。

设计要点：
- 读数先按温度段（同温度段）扣减历史基线，再与限值比较，避免昼夜温差造成的
  索力缓变被单跳点误判。
- 仅"连续三笔"已采纳读数的扣减索力仍高于限值时才进入严重告警（warning）。
- 传感器离线、采集时间倒置、温度缺失的读数只标记为"待核"，不参与任何统计，
  也不改变当前建议。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# 同温度段宽度（摄氏度）：[n*WIDTH, (n+1)*WIDTH)
TEMP_BIN_WIDTH = 5.0
# 连续多少笔扣减后仍超限才升级为严重告警
CONSECUTIVE_LIMIT = 3

CABLE_ITEM_TYPE = "cable_point"
GENERIC_ITEM_TYPE = "generic"

# 待核原因
PENDING_OFFLINE = "sensor_offline"        # 传感器离线 / 索力缺失
PENDING_TIME_INVERSION = "time_inversion"  # 采集时刻早于或等于已采纳读数
PENDING_MISSING_TEMP = "missing_temperature"  # 温度缺失

PENDING_REASONS = (PENDING_OFFLINE, PENDING_TIME_INVERSION, PENDING_MISSING_TEMP)

# 基线扣减依据
BASELINE_COLD_START = "cold_start_seed"   # 该温度段首笔，以自身为种子，偏差记0
BASELINE_SEGMENT_MEAN = "segment_mean"    # 同温度段正常读数均值


def parse_timestamp(value: Any, field: str = "read_at") -> datetime:
    """解析ISO-8601采集时刻；朴素时间按UTC处理；返回aware datetime。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}必须是ISO-8601时间字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}必须是ISO-8601时间字符串") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def temperature_bin_index(temperature: float, width: float = TEMP_BIN_WIDTH) -> int:
    return int(math.floor(temperature / width))


def temperature_bin_label(temperature: float, width: float = TEMP_BIN_WIDTH) -> str:
    idx = temperature_bin_index(temperature, width)
    low = idx * width
    high = low + width
    if float(low).is_integer():
        low = int(low)
    if float(high).is_integer():
        high = int(high)
    return f"{low}~{high}"


def detect_pending_reasons(force: Any, temperature: Any, offline: bool,
                           read_at: Optional[datetime],
                           prior_accepted: List[Dict[str, Any]]) -> List[str]:
    """按固定顺序判定待核原因；多个原因同时存在时全部列出。

    待核读数不参与基线和连续计数，也不改变当前建议。
    """
    reasons: List[str] = []
    if offline or force is None:
        reasons.append(PENDING_OFFLINE)
    if read_at is not None and prior_accepted:
        last_ts = prior_accepted[-1]["read_at_dt"]
        if read_at <= last_ts:
            reasons.append(PENDING_TIME_INVERSION)
    if temperature is None:
        reasons.append(PENDING_MISSING_TEMP)
    return reasons


def compute_baseline(temperature: float, force: float,
                     prior_accepted: List[Dict[str, Any]],
                     upper_limit: float,
                     width: float = TEMP_BIN_WIDTH
                     ) -> Tuple[float, int, str, float, bool]:
    """计算同温度段基线与扣减索力。

    仅使用同温度段内"历史正常读数"（当时扣减后未超限）的原始索力均值，避免
    异常样本污染基线。温度段首笔采用冷启动：以自身索力为种子、偏差记0。

    返回 (baseline, samples, method, corrected_force, excess)。
    """
    label = temperature_bin_label(temperature, width)
    samples = [
        r for r in prior_accepted
        if r.get("temp_bin") == label
        and r.get("corrected_force") is not None
        and not r.get("excess")
    ]
    if not samples:
        return force, 0, BASELINE_COLD_START, 0.0, False
    baseline = sum(float(r["force"]) for r in samples) / len(samples)
    corrected = force - baseline
    excess = corrected > upper_limit
    return baseline, len(samples), BASELINE_SEGMENT_MEAN, corrected, excess


def next_streak(prior_accepted: List[Dict[str, Any]], excess: bool) -> int:
    """连续超限笔数：本笔超限则在历史连续值上+1，否则清零。待核读数不进入此序列。"""
    if not excess:
        return 0
    last = prior_accepted[-1] if prior_accepted else None
    return int(last["streak"]) + 1 if last and last.get("streak") else 1


def notice_is_effective(record: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[bool, str]:
    """交通通告是否有效：记录未关闭（未解除）且当前时间落在有效期内。"""
    if record.get("status") != "open":
        return False, "已解除"
    now = now or datetime.now(timezone.utc)
    valid_from = record.get("valid_from")
    valid_until = record.get("valid_until")
    if valid_from:
        start = parse_timestamp(valid_from, "valid_from")
        if now < start:
            return False, "尚未生效"
    if valid_until:
        end = parse_timestamp(valid_until, "valid_until")
        if now > end:
            return False, "已过有效期"
    return True, ""
