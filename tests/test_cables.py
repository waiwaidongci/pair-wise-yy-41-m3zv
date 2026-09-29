import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import (CONSECUTIVE_LIMIT, PENDING_BASELINE_MISSING,
                       PENDING_MISSING_TEMPERATURE, PENDING_OFFLINE,
                       PENDING_TIME_INVERSION, adjust_force,
                       consecutive_over_limit, find_baseline)
from src.service import Service


def make_item(service, ref="CABLE-1", threshold=10.0):
    return service.create_item({
        "title": "斜拉索C1", "description": "索力缓变识别",
        "severity": "normal", "quantity": 0, "threshold": threshold,
        "external_ref": ref,
    }, "op", "sensor_operator")


class CableRuleTest(unittest.TestCase):
    def test_find_baseline_left_closed_right_open(self):
        baselines = [
            {"id": 1, "tmin": -10.0, "tmax": 0.0, "baseline_force": 100.0},
            {"id": 2, "tmin": 0.0, "tmax": 10.0, "baseline_force": 95.0},
            {"id": 3, "tmin": 10.0, "tmax": None, "baseline_force": 90.0},
        ]
        self.assertEqual(find_baseline(baselines, 0.0)["id"], 2)
        self.assertEqual(find_baseline(baselines, -10.0)["id"], 1)
        self.assertEqual(find_baseline(baselines, 25.0)["id"], 3)
        self.assertIsNone(find_baseline(baselines, -20.0))

    def test_adjust_and_consecutive(self):
        base = {"id": 2, "tmin": 0.0, "tmax": 10.0, "baseline_force": 95.0}
        basis = adjust_force(110.0, 5.0, base)
        self.assertAlmostEqual(basis["force_adjusted"], 15.0)
        rows = [{"status": "accepted", "force_adjusted": 11.0}] * CONSECUTIVE_LIMIT
        self.assertTrue(consecutive_over_limit(rows, 10.0))
        rows[-1] = {"status": "accepted", "force_adjusted": 9.0}
        self.assertFalse(consecutive_over_limit(rows, 10.0))
        self.assertFalse(consecutive_over_limit(rows[:2], 10.0))


class CableWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = make_item(self.service)
        # 0~10℃ 段基线 100kN，限值10kN
        self.service.add_baseline(self.item["id"], {
            "tmin": 0, "tmax": 10, "baseline_force": 100,
        }, "op", "sensor_operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def reading(self, force, temp, ts, actor="op"):
        return self.service.submit_reading(self.item["id"], {
            "force": force, "temperature": temp, "measured_at": ts,
        }, actor, "sensor_operator")

    def test_baseline_subtracted_single_spike_no_critical(self):
        # 温度缓变：110-100=10 不超限；112-100=12 仅一笔，不应升级
        r1 = self.reading(110, 5.0, "2026-09-28T20:00:00Z")
        self.assertEqual(r1["status"], "accepted")
        self.assertAlmostEqual(r1["force_adjusted"], 10.0)
        self.assertEqual(r1["over_limit"], 0)
        self.assertNotIn("alarm", r1)
        r2 = self.reading(112, 5.0, "2026-09-28T21:00:00Z")
        self.assertEqual(r2["over_limit"], 1)
        self.assertNotIn("alarm", r2)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["severity"], "normal")
        self.assertEqual(item["status"], "normal")

    def test_three_consecutive_adjusted_over_limit_raises_severe(self):
        self.reading(112, 5.0, "2026-09-28T20:00:00Z")
        self.reading(113, 6.0, "2026-09-28T21:00:00Z")
        third = self.reading(114, 7.0, "2026-09-28T22:00:00Z")
        self.assertIn("alarm", third)
        alarm = third["alarm"]
        self.assertEqual(alarm["status"], "open")
        self.assertEqual(len(alarm["evidence"]["readings"]), 3)
        # 原始读数与扣减依据都保留
        for ev in alarm["evidence"]["readings"]:
            self.assertIn("force", ev)
            self.assertIn("baseline_force", ev)
            self.assertIn("force_adjusted", ev)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["severity"], "critical")
        self.assertEqual(item["status"], "normal")
        self.assertTrue(item["escalation_required"])

    def test_pending_readings_do_not_change_recommendation(self):
        # 离线
        p1 = self.service.submit_reading(self.item["id"], {
            "offline": True, "measured_at": "2026-09-28T20:00:00Z",
        }, "op", "sensor_operator")
        self.assertEqual(p1["status"], "pending")
        self.assertEqual(p1["pending_reason"], PENDING_OFFLINE)
        # 温度缺失
        p2 = self.reading(130, None, "2026-09-28T20:05:00Z")
        self.assertEqual(p2["pending_reason"], PENDING_MISSING_TEMPERATURE)
        # 时间倒置
        self.reading(105, 5.0, "2026-09-28T22:00:00Z")
        p3 = self.reading(106, 5.0, "2026-09-28T21:00:00Z")
        self.assertEqual(p3["pending_reason"], PENDING_TIME_INVERSION)
        # 无匹配温度段
        p4 = self.reading(130, -25.0, "2026-09-28T23:00:00Z")
        self.assertEqual(p4["pending_reason"], PENDING_BASELINE_MISSING)
        pending = self.service.list_readings(self.item["id"], "viewer", "pending")
        self.assertEqual(len(pending), 4)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["severity"], "normal")
        self.assertEqual(item["status"], "normal")

    def test_shift_handover_sees_raw_and_basis(self):
        self.reading(112, 5.0, "2026-09-28T20:00:00Z")
        readings = self.service.list_readings(self.item["id"], "viewer")
        r = readings[0]
        self.assertEqual(r["force"], 112)                       # 原读数
        self.assertEqual(r["temperature"], 5.0)
        self.assertEqual(r["measured_at_raw"], "2026-09-28T20:00:00Z")
        self.assertEqual(r["baseline_force"], 100)              # 扣减依据
        self.assertEqual(r["band_tmin"], 0)
        self.assertEqual(r["band_tmax"], 10)
        self.assertAlmostEqual(r["force_adjusted"], 12)

    def test_four_eyes_review_then_notice_gate(self):
        self.reading(112, 5.0, "2026-09-28T20:00:00Z")
        self.reading(113, 5.0, "2026-09-28T21:00:00Z")
        third = self.reading(114, 5.0, "2026-09-28T22:00:00Z")
        alarm_id = third["alarm"]["id"]
        version = self.service.get_item(self.item["id"], "viewer")["version"]
        # 复核必须换人
        with self.assertRaises(PermissionDenied):
            self.service.review_alarm(self.item["id"], alarm_id, {
                "decision": "confirmed", "note": "本人复核",
                "expected_version": version,
            }, "op", "bridge_engineer")
        # 另一名工程师确认异常 -> warning（通告非该步前提）
        result = self.service.review_alarm(self.item["id"], alarm_id, {
            "decision": "confirmed", "note": "复核确认异常",
            "expected_version": version,
        }, "eng2", "bridge_engineer")
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["alarm"]["reviewed_by"], "eng2")

        version = result["version"]
        # warning -> restricted
        current = self.service.transition(self.item["id"], "restricted",
                                          version, "eng2", "bridge_engineer")
        version = current["version"]
        # 通告缺失 -> 保留 restricted 并说明原因
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"], "closed", version,
                                    "ta", "traffic_authority")
        blocked = [e for e in self.service.audit("viewer", self.item["id"])
                   if e["action"] == "transition_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[-1]["detail"]["reason"],
                         "traffic_notice_missing_or_lifted")
        # 关联有效通告后可封闭
        self.service.add_traffic_notice(self.item["id"], {
            "detail": "封闭绕行", "external_ref": "TN-9",
        }, "ta", "traffic_authority")
        closed = self.service.transition(self.item["id"], "closed", version,
                                         "ta", "traffic_authority")
        self.assertEqual(closed["status"], "closed")
        # 通告解除后无法再凭已解除通告封闭（状态已是 closed，闸门直接校验计数）
        self.service.add_traffic_notice(self.item["id"], {
            "detail": "第二条", "external_ref": "TN-10",
        }, "ta", "traffic_authority")
        notices = [r for r in self.service.list_records(self.item["id"], "viewer")
                   if r["kind"] == "traffic_notice"]
        self.service.lift_traffic_notice(self.item["id"],
                                         notices[0]["id"], "ta",
                                         "traffic_authority")

    def test_rejected_alarm_keeps_original_status(self):
        self.reading(112, 5.0, "2026-09-28T20:00:00Z")
        self.reading(113, 5.0, "2026-09-28T21:00:00Z")
        third = self.reading(114, 5.0, "2026-09-28T22:00:00Z")
        alarm_id = third["alarm"]["id"]
        version = self.service.get_item(self.item["id"], "viewer")["version"]
        result = self.service.review_alarm(self.item["id"], alarm_id, {
            "decision": "rejected", "note": "确认是标定干扰",
            "expected_version": version,
        }, "eng2", "bridge_engineer")
        self.assertEqual(result["status"], "normal")
        self.assertEqual(result["alarm"]["status"], "rejected")
        # 读数仍保留
        self.assertEqual(len(self.service.list_readings(self.item["id"], "viewer")), 3)
        # 拒绝后新三笔才会再次告警（被拒批次不计入）
        self.reading(111, 5.0, "2026-09-28T23:00:00Z")
        self.reading(111, 5.0, "2026-09-29T00:00:00Z")
        again = self.reading(111, 5.0, "2026-09-29T01:00:00Z")
        self.assertIn("alarm", again)

    def test_lifted_notice_blocks_closure(self):
        # 另起一个测点直接走到 restricted 再验证“已解除”
        item = make_item(self.service, "CABLE-2")
        self.service.add_baseline(item["id"], {
            "tmin": 0, "tmax": 10, "baseline_force": 100,
        }, "op", "sensor_operator")
        notice = self.service.add_traffic_notice(item["id"], {
            "detail": "临时通告", "external_ref": "TN-L1",
        }, "ta", "traffic_authority")
        self.service.lift_traffic_notice(item["id"], notice["id"], "ta",
                                         "traffic_authority")
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"], "closed", 1, "ta",
                                    "traffic_authority")

    def test_bad_payloads_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.submit_reading(self.item["id"], {
                "force": 120, "temperature": 5,
                "measured_at": "not-a-time",
            }, "op", "sensor_operator")
        with self.assertRaises(PermissionDenied):
            self.service.submit_reading(self.item["id"], {
                "force": 120, "temperature": 5,
                "measured_at": "2026-09-28T20:00:00Z",
            }, "op", "viewer")
        with self.assertRaises(ValidationError):
            self.service.submit_reading(self.item["id"], {
                "temperature": 5, "measured_at": "2026-09-28T20:00:00Z",
            }, "op", "sensor_operator")


if __name__ == "__main__":
    unittest.main()
