from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (ALARM_RECORD_KIND, AUDIT_ROLES, CONSECUTIVE_LIMIT, CREATE_ROLES,
                    ENTITY, PENDING_BASELINE_MISSING, PENDING_MISSING_TEMPERATURE,
                    PENDING_OFFLINE, PENDING_TIME_INVERSION, READING_ACCEPTED,
                    READING_PENDING, RECORD_ROLES, TRAFFIC_NOTICE_KIND,
                    VIEW_ROLES, adjust_force, completion_blockers,
                    consecutive_over_limit, escalation_required, find_baseline,
                    is_over_limit, parse_measured_at, priority_score,
                    response_deadline_hours, role_for_transition,
                    traffic_notice_gate, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

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
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        # 封闭必须绑定有效交通通告；缺失或已解除时保留原状态并说明原因
        notice_count = self.repository.open_traffic_notice_count(item_id)
        try:
            traffic_notice_gate(target, notice_count)
        except ConflictError:
            self.repository.append_audit("transition_blocked", ENTITY, item_id, actor, {
                "from": item["status"], "to": target,
                "open_notice_count": notice_count,
                "reason": "traffic_notice_missing_or_lifted",
            })
            raise
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
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

    # ===== 温度段基线 =====
    BASELINE_ROLES = {"sensor_operator", "bridge_engineer"}

    def add_baseline(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, self.BASELINE_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        tmin = require_number(payload.get("tmin"), "tmin", minimum=None)
        tmax = payload.get("tmax")
        if tmax is not None:
            tmax = require_number(tmax, "tmax", minimum=None)
            if tmax <= tmin:
                raise ValidationError("tmax必须大于tmin")
        baseline_force = require_number(payload.get("baseline_force"),
                                        "baseline_force", minimum=None)
        baseline = self.repository.add_baseline(item_id, tmin, tmax,
                                                baseline_force, actor)
        self.repository.append_audit("baseline", ENTITY, item_id, actor, {
            "baseline_id": baseline["id"], "tmin": tmin, "tmax": tmax,
            "baseline_force": baseline_force,
        })
        return baseline

    def list_baselines(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_baselines(item_id)

    # ===== 索力读数（缓变识别） =====
    def submit_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, {"sensor_operator"})
        actor = require_text(actor, "actor", 100)
        measured_raw = payload.get("measured_at")
        measured_dt = parse_measured_at(measured_raw)
        measured_iso = measured_dt.isoformat()

        common = {"item_id": item_id, "measured_at": measured_iso,
                  "measured_at_raw": str(measured_raw).strip(),
                  "created_by": actor}

        def pending(reason: str, force=None, temperature=None) -> Dict[str, Any]:
            data = dict(common)
            data.update(status=READING_PENDING, pending_reason=reason,
                        force=force, temperature=temperature)
            reading = self.repository.insert_reading(data)
            self.repository.append_audit("reading_pending", ENTITY, item_id, actor, {
                "reading_id": reading["id"], "pending_reason": reason,
                "force": force, "temperature": temperature,
                "measured_at": measured_iso,
            })
            return reading

        # 1) 传感器离线 -> 只留待核
        if bool(payload.get("offline", False)):
            return pending(PENDING_OFFLINE)

        # 索力缺失或非数字 -> 422（读数本身不完整）；离线已在前面放行
        force = payload.get("force")
        if force is None:
            raise ValidationError("force不能为空（传感器离线请提交offline=true）")
        force = require_number(force, "force")

        # 2) 温度缺失 -> 只留待核
        temperature = payload.get("temperature")
        if temperature is None:
            return pending(PENDING_MISSING_TEMPERATURE, force=force)
        temperature = require_number(temperature, "temperature", minimum=None)

        # 3) 时间倒置 -> 只留待核（与最近一笔有效读数比较）
        last = self.repository.last_accepted_reading(item_id)
        if last is not None and measured_iso < last["measured_at"]:
            reading = pending(PENDING_TIME_INVERSION, force=force,
                              temperature=temperature)
            reading["last_accepted_at"] = last["measured_at"]
            return reading

        # 4) 同温度段基线缺失 -> 只留待核，不参与计数
        baselines = self.repository.list_baselines(item_id)
        baseline = find_baseline(baselines, temperature)
        if baseline is None:
            return pending(PENDING_BASELINE_MISSING, force=force,
                           temperature=temperature)

        # 5) 扣同温度段基线
        item = self.repository.get_item(item_id)
        basis = adjust_force(force, temperature, baseline)
        over = is_over_limit(basis["force_adjusted"], item["threshold"])
        data = dict(common)
        data.update(status=READING_ACCEPTED, force=force, temperature=temperature,
                    baseline_id=basis["baseline_id"], band_tmin=basis["band_tmin"],
                    band_tmax=basis["band_tmax"], baseline_force=basis["baseline_force"],
                    force_adjusted=basis["force_adjusted"], over_limit=over)
        reading = self.repository.insert_reading(data)
        self.repository.append_audit("reading", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "force": force,
            "temperature": temperature, "measured_at": measured_iso,
            "baseline_id": basis["baseline_id"],
            "baseline_force": basis["baseline_force"],
            "force_adjusted": basis["force_adjusted"], "over_limit": over,
        })

        # 6) 连续三笔扣减后仍高于限值 -> 严重告警（单次跳点不会触发）
        alarm = None
        if over:
            streak = self.repository.recent_accepted_readings(
                item_id, CONSECUTIVE_LIMIT)
            if consecutive_over_limit(streak, item["threshold"]):
                evidence = {
                    "reason": "consecutive_over_limit",
                    "required": CONSECUTIVE_LIMIT,
                    "readings": [
                        {"reading_id": r["id"], "force": r["force"],
                         "temperature": r["temperature"],
                         "measured_at": r["measured_at"],
                         "baseline_id": r["baseline_id"],
                         "baseline_force": r["baseline_force"],
                         "band_tmin": r["band_tmin"], "band_tmax": r["band_tmax"],
                         "force_adjusted": r["force_adjusted"]}
                        for r in streak
                    ],
                }
                # 告警依据同时落一条记录，供换班后查看
                record = self.repository.add_record(
                    item_id, ALARM_RECORD_KIND,
                    "连续%d笔扣减同温度段基线后仍高于限值" % CONSECUTIVE_LIMIT,
                    "open", None, actor)
                alarm = self.repository.raise_severe_alarm(
                    item_id, item["version"], evidence,
                    [r["id"] for r in streak], actor, record["id"])
                self.repository.append_audit("severe_alarm", ENTITY, item_id, actor, {
                    "alarm_id": alarm["id"], "record_id": record["id"],
                    "reading_ids": [r["id"] for r in streak],
                })
        reading = self.repository.get_reading(reading["id"])
        if alarm is not None:
            reading["alarm"] = alarm
        return reading

    def list_readings(self, item_id: int, role: str,
                      status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in (READING_ACCEPTED, READING_PENDING):
            raise ValidationError("status必须是accepted或pending")
        return self.repository.list_readings(item_id, status)

    # ===== 人工复核（换人） =====
    def review_alarm(self, item_id: int, alarm_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, {"bridge_engineer"})
        actor = require_text(actor, "actor", 100)
        note = require_text(payload.get("note"), "note")
        decision = payload.get("decision")
        if decision not in ("confirmed", "rejected"):
            raise ValidationError("decision必须是confirmed或rejected")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        alarm = self.repository.get_alarm(alarm_id)
        if alarm["item_id"] != item_id:
            raise NotFoundError("告警不属于该测点")
        item = self.repository.get_item(item_id)
        if item["version"] != expected_version:
            raise ConflictError("版本冲突，请刷新后重试")
        if alarm["status"] != "open":
            raise ConflictError("告警已复核，不能重复确认")
        # 人工复核必须换人
        if actor == alarm["raised_by"]:
            raise PermissionDenied("人工复核必须由提报人以外的桥梁工程师执行")

        record_id = alarm.get("record_id")
        if decision == "confirmed":
            validate_transition(item["status"], "warning")
            # 确认异常后再关联有效交通通告（若已有）
            notice_count = self.repository.open_traffic_notice_count(item_id)
            updated = self.repository.transition_item(
                item_id, "warning", expected_version, actor)
            alarm = self.repository.review_alarm(alarm_id, "confirmed", actor,
                                                 note, record_id)
            if record_id is not None:
                self.repository.close_record(record_id, actor)
            self.repository.append_audit("alarm_confirm", ENTITY, item_id, actor, {
                "alarm_id": alarm_id, "from": item["status"], "to": "warning",
                "open_notice_count": notice_count,
            })
            result = self.enrich(updated)
            result["alarm"] = alarm
            return result

        # rejected：保留原状态，说明原因；读数仍在，连续计数随告警复核重新开始
        alarm = self.repository.review_alarm(alarm_id, "rejected", actor,
                                             note, record_id)
        if record_id is not None:
            self.repository.close_record(record_id, actor)
        self.repository.append_audit("alarm_reject", ENTITY, item_id, actor, {
            "alarm_id": alarm_id, "status": item["status"], "note": note,
        })
        result = self.enrich(self.repository.get_item(item_id))
        result["alarm"] = alarm
        return result

    def get_alarm(self, item_id: int, alarm_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        alarm = self.repository.get_alarm(alarm_id)
        if alarm["item_id"] != item_id:
            raise NotFoundError("告警不属于该测点")
        return alarm

    # ===== 交通通告 =====
    def add_traffic_notice(self, item_id: int, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, {"traffic_authority"})
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        external_ref = require_text(payload.get("external_ref"),
                                    "external_ref", 100)
        record = self.repository.add_record(
            item_id, TRAFFIC_NOTICE_KIND, detail, "open", external_ref, actor)
        self.repository.append_audit("traffic_notice", ENTITY, item_id, actor, {
            "record_id": record["id"], "external_ref": external_ref,
            "status": "open",
        })
        return record

    def lift_traffic_notice(self, item_id: int, record_id: int, actor: str,
                            role: str) -> Dict[str, Any]:
        ensure_role(role, {"traffic_authority"})
        actor = require_text(actor, "actor", 100)
        record = self.repository.get_record(record_id)
        if record["item_id"] != item_id or record["kind"] != TRAFFIC_NOTICE_KIND:
            raise NotFoundError("交通通告不存在")
        updated = self.repository.close_record(record_id, actor)
        self.repository.append_audit("traffic_notice_lift", ENTITY, item_id, actor, {
            "record_id": record_id, "external_ref": updated["external_ref"],
        })
        return updated

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
