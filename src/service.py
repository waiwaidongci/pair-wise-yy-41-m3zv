from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import cable
from .cable import (BASELINE_COLD_START, CABLE_ITEM_TYPE, CONSECUTIVE_LIMIT,
                    compute_baseline, detect_pending_reasons, format_ts,
                    next_streak, notice_is_effective, parse_timestamp,
                    temperature_bin_label)
from .domain import (ConflictError, NotFoundError, PermissionDenied,
                     ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)

ITEM_TYPES = (CABLE_ITEM_TYPE, cable.GENERIC_ITEM_TYPE)
TRAFFIC_NOTICE_KIND = "traffic_notice"
TRAFFIC_NOTICE_ROLES = ("traffic_authority",)
NOTICE_CLOSE_ROLES = ("traffic_authority", "bridge_engineer")


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def _get_cable_item(self, item_id: int) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        if item.get("item_type") != CABLE_ITEM_TYPE:
            raise ValidationError("该测点不是斜拉索测点，不能提交索力读数")
        return item

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item_type = payload.get("item_type", cable.GENERIC_ITEM_TYPE)
        if item_type not in ITEM_TYPES:
            raise ValidationError("item_type不在允许范围内")
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, item_type)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "item_type": item_type,
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        kind = require_text(payload.get("kind"), "kind", 100)
        is_notice = kind == TRAFFIC_NOTICE_KIND
        if is_notice:
            ensure_role(role, TRAFFIC_NOTICE_ROLES)
        else:
            ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        valid_from = valid_until = None
        if payload.get("valid_from") is not None:
            valid_from = format_ts(parse_timestamp(payload["valid_from"], "valid_from"))
        if payload.get("valid_until") is not None:
            valid_until = format_ts(parse_timestamp(payload["valid_until"], "valid_until"))
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, valid_from, valid_until)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "external_ref": external_ref,
        })
        return record

    def close_record(self, item_id: int, record_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        """解除（关闭）一条记录，例如交通通告解除。"""
        self._view(role)
        ensure_role(role, NOTICE_CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        record = self.repository.get_record(record_id)
        if record["item_id"] != item_id:
            raise NotFoundError("记录不存在")
        updated = self.repository.close_record(record_id, actor)
        self.repository.append_audit("record_close", ENTITY, item_id, actor, {
            "record_id": record_id, "kind": record["kind"],
            "external_ref": record["external_ref"],
        })
        return updated

    def submit_reading(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """提交一笔索力/温度/采集时刻读数。

        - 先扣同温度段基线，再与限值比较；
        - 连续三笔已采纳读数扣减后仍超限，测点才进入严重告警(warning)；
        - 离线、时间倒置、温度缺失只留待核，不改变当前建议。
        """
        ensure_role(role, ("sensor_operator",))
        actor = require_text(actor, "actor", 100)
        item = self._get_cable_item(item_id)

        offline = bool(payload.get("offline", False))
        raw_force = payload.get("force", None)
        force: Optional[float] = None
        if raw_force is not None:
            force = require_number(raw_force, "force")
        temperature_raw = payload.get("temperature", None)
        temperature: Optional[float] = None
        if temperature_raw is not None:
            temperature = require_number(temperature_raw, "temperature",
                                         minimum=float("-inf"))
        read_at_raw = payload.get("read_at")
        read_at = parse_timestamp(read_at_raw, "read_at") if read_at_raw is not None else None
        if read_at is None and not offline:
            raise ValidationError("read_at（采集时刻）不能为空")

        prior = self.repository.accepted_readings(item_id)
        reasons = detect_pending_reasons(raw_force, temperature_raw, offline,
                                         read_at, prior)

        from .audit import utc_now
        base: Dict[str, Any] = {
            "item_id": item_id, "offline": offline,
            "read_at": format_ts(read_at) if read_at else None,
            "ingested_at": utc_now(), "created_by": actor,
            "raw_force": force,
        }

        advice_changed = False
        if reasons:
            # 只留待核：不扣基线、不参与连续计数、不改当前建议
            reading = self.repository.add_reading(dict(
                base, force=None, temperature=temperature, state="pending",
                pending_reasons=reasons,
            ))
            self.repository.append_audit("reading_pending", ENTITY, item_id, actor, {
                "reading_id": reading["id"], "reasons": reasons,
                "read_at": reading["read_at"], "raw_force": force,
            })
        else:
            bin_label = temperature_bin_label(temperature)
            baseline, samples, method, corrected, excess = compute_baseline(
                temperature, force, prior, item["threshold"])
            streak = next_streak(prior, excess)
            reading = self.repository.add_reading(dict(
                base, force=force, temperature=temperature, state="accepted",
                temp_bin=bin_label, baseline=baseline, baseline_samples=samples,
                baseline_method=method, corrected_force=corrected,
                excess=excess, streak=streak,
            ))
            audit_detail = {
                "reading_id": reading["id"], "force": force,
                "temperature": temperature, "temp_bin": bin_label,
                "baseline": baseline, "baseline_samples": samples,
                "baseline_method": method, "corrected_force": corrected,
                "limit": item["threshold"], "excess": excess, "streak": streak,
            }
            # 连续三笔仍高于限值才进入严重告警；且仅在正常态自动升级一次
            if streak >= CONSECUTIVE_LIMIT and item["status"] == "normal":
                updated = self.repository.transition_item(
                    item_id, "warning", item["version"], actor, raised_by=actor)
                item = updated
                advice_changed = True
                audit_detail["auto_severe"] = True
                audit_detail["to_status"] = "warning"
            self.repository.append_audit("reading", ENTITY, item_id, actor, audit_detail)

        result = dict(reading)
        result["pending"] = reading["state"] == "pending"
        result["advice_changed"] = advice_changed
        result["item_status"] = item["status"]
        result["deduction"] = self._deduction_note(reading)
        return result

    @staticmethod
    def _deduction_note(reading: Dict[str, Any]) -> Optional[str]:
        """换班后可见的扣减依据说明。"""
        if reading["state"] != "accepted":
            return None
        if reading["baseline_method"] == BASELINE_COLD_START:
            return (f"温度段{reading['temp_bin']}首笔读数，以原始索力"
                    f"{reading['force']}为基线种子，偏差记0")
        return (f"同温度段{reading['temp_bin']}正常读数{reading['baseline_samples']}笔，"
                f"基线索力{round(reading['baseline'], 4)}，"
                f"扣减后{round(reading['corrected_force'], 4)}")

    def list_readings(self, item_id: int, role: str) -> Dict[str, Any]:
        """换班视图：原读数与扣减依据全部可查。"""
        self._view(role)
        self._get_cable_item(item_id)
        readings = self.repository.list_readings(item_id)
        for r in readings:
            r["deduction"] = self._deduction_note(r)
            r["pending"] = r["state"] == "pending"
        return {
            "readings": readings,
            "pending_count": self.repository.pending_reading_count(item_id),
        }

    def manual_review(self, item_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        """人工复核：必须换人（区别于触发严重告警的提交人）。"""
        ensure_role(role, ("bridge_engineer",))
        actor = require_text(actor, "actor", 100)
        item = self._get_cable_item(item_id)
        if item["status"] != "warning":
            raise ConflictError("仅严重告警(warning)状态需要人工复核")
        raised_by = item.get("raised_by")
        if raised_by and actor == raised_by:
            raise PermissionDenied("人工复核必须换人，不能由告警提交人自行复核")
        anomaly = payload.get("anomaly")
        if not isinstance(anomaly, bool):
            raise ValidationError("anomaly必须是布尔值")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note")
        review = self.repository.add_review(item_id, actor, anomaly, note)
        self.repository.append_audit("manual_review", ENTITY, item_id, actor, {
            "review_id": review["id"], "anomaly": anomaly,
            "different_from": raised_by,
        })
        if not anomaly:
            # 复核不确认异常：保留原状态
            review["item_status"] = item["status"]
            review["note_status"] = "未确认异常，保留严重告警状态"
            return review
        review["item_status"] = item["status"]
        review["note_status"] = "异常已确认，需关联有效交通通告后方可限行"
        return review

    def list_reviews(self, item_id: int, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        self._get_cable_item(item_id)
        return self.repository.list_reviews(item_id)

    def _cable_transition_gate(self, item: Dict[str, Any], target: str,
                               payload: Dict[str, Any]) -> None:
        """斜拉索测点限行/封闭前：确认异常的复核 + 有效交通通告，缺一保留原状态。"""
        if target not in ("restricted", "closed"):
            return
        review = self.repository.confirmed_review(item["id"])
        if review is None:
            raise ConflictError("尚未经换人复核确认异常，保留原状态")
        notice_ref = payload.get("notice_ref")
        if notice_ref is None:
            raise ConflictError("限行/封闭决策必须关联交通通告；通告缺失，保留原状态")
        notice_ref = require_text(notice_ref, "notice_ref", 100)
        record = self.repository.find_record_by_ref(item["id"], notice_ref)
        if record is None or record["kind"] != TRAFFIC_NOTICE_KIND:
            raise ConflictError(f"交通通告{notice_ref}不存在，保留原状态")
        effective, reason = notice_is_effective(record)
        if not effective:
            raise ConflictError(f"交通通告{notice_ref}{reason}，保留原状态")

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str, payload: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if item.get("item_type") == CABLE_ITEM_TYPE:
            self._cable_transition_gate(item, target, payload or {})
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        audit_detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if item.get("item_type") == CABLE_ITEM_TYPE and target in ("restricted", "closed"):
            audit_detail["notice_ref"] = (payload or {}).get("notice_ref")
            audit_detail["review_confirmed"] = True
        self.repository.append_audit("transition", ENTITY, item_id, actor, audit_detail)
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        if item.get("item_type") == CABLE_ITEM_TYPE:
            result["monitor"] = self._monitor_summary(item)
        return result

    def _monitor_summary(self, item: Dict[str, Any]) -> Dict[str, Any]:
        accepted = self.repository.accepted_readings(item["id"])
        latest = accepted[-1] if accepted else None
        return {
            "limit": item["threshold"],
            "consecutive_required": CONSECUTIVE_LIMIT,
            "accepted_count": len(accepted),
            "pending_count": self.repository.pending_reading_count(item["id"]),
            "latest": self._latest_summary(latest),
            "raised_by": item.get("raised_by"),
        }

    @staticmethod
    def _latest_summary(reading: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if reading is None:
            return None
        return {
            "read_at": reading["read_at"],
            "force": reading["force"],
            "temperature": reading["temperature"],
            "temp_bin": reading["temp_bin"],
            "baseline": reading["baseline"],
            "corrected_force": reading["corrected_force"],
            "excess": reading["excess"],
            "streak": reading["streak"],
        }
