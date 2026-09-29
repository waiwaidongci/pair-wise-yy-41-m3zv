import tempfile
import unittest
from pathlib import Path

from src.domain import (ConflictError, PermissionDenied, ValidationError)
from src.repository import Repository
from src.service import Service

OP = "operator_zhang"
ENG = "engineer_li"
ENG2 = "engineer_wang"
TA = "traffic_chen"


def t(hour, day=28):
    return f"2026-09-{day:02d}T{hour:02d}:00:00+00:00"


class CableServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "N7号斜拉索", "description": "北侧斜拉索测点",
            "severity": "normal", "quantity": 0, "threshold": 50.0,
            "external_ref": "CABLE-N7", "item_type": "cable_point",
        }, OP, "sensor_operator")
        self.iid = self.item["id"]
        # 先在10~15温度段建立基线（原始索力约5001）
        self.service.submit_reading(self.iid, {
            "force": 5000.0, "temperature": 12.0, "read_at": t(0)}, OP, "sensor_operator")
        self.service.submit_reading(self.iid, {
            "force": 5002.0, "temperature": 12.4, "read_at": t(1)}, OP, "sensor_operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def reading(self, force, temp, hour, **extra):
        payload = {"force": force, "temperature": temp, "read_at": t(hour)}
        payload.update(extra)
        return self.service.submit_reading(self.iid, payload, OP, "sensor_operator")

    def test_slow_temperature_drift_does_not_alarm(self):
        # 换温度段（0~5）冷启动，索力随温度缓变但扣减后平稳
        r1 = self.reading(4800.0, 3.0, 2)
        self.assertFalse(r1["excess"])
        r2 = self.reading(4801.0, 3.1, 3)
        self.assertLess(r2["corrected_force"], 50.0)
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"], "normal")

    def test_single_spike_does_not_close(self):
        r = self.reading(5300.0, 12.0, 4)  # 单次跳点
        self.assertTrue(r["excess"])
        self.assertEqual(r["streak"], 1)
        self.assertFalse(r["advice_changed"])
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"], "normal")

    def test_three_consecutive_excess_enter_warning(self):
        self.reading(5300.0, 12.0, 4)
        self.reading(5003.0, 12.1, 5)  # 恢复，连续计数清零
        self.reading(5300.0, 12.2, 6)
        self.reading(5301.0, 12.3, 7)
        r = self.reading(5302.0, 12.4, 8)
        self.assertEqual(r["streak"], 3)
        self.assertTrue(r["advice_changed"])
        item = self.service.get_item(self.iid, "viewer")
        self.assertEqual(item["status"], "warning")
        self.assertEqual(item["monitor"]["latest"]["streak"], 3)
        self.assertEqual(item["raised_by"], OP)

    def test_pending_readings_are_kept_and_do_not_change_advice(self):
        self.reading(5300.0, 12.0, 4)   # streak 1
        off = self.reading(None, 12.0, 5, offline=True)
        self.assertTrue(off["pending"])
        self.assertIn("sensor_offline", off["pending_reasons"])
        inv = self.reading(5300.0, 12.0, 3)  # 时间倒置
        self.assertTrue(inv["pending"])
        self.assertIn("time_inversion", inv["pending_reasons"])
        no_temp = self.reading(5300.0, None, 6)
        self.assertTrue(no_temp["pending"])
        self.assertIn("missing_temperature", no_temp["pending_reasons"])
        self.reading(5301.0, 12.1, 7)   # streak 2（待核读数不打断也不计入）
        still = self.service.get_item(self.iid, "viewer")
        self.assertEqual(still["status"], "normal")
        self.assertEqual(still["monitor"]["pending_count"], 3)
        r = self.reading(5302.0, 12.2, 8)  # streak 3
        self.assertTrue(r["advice_changed"])

    def test_deduction_note_visible_after_shift(self):
        self.reading(5300.0, 12.0, 4)
        view = self.service.list_readings(self.iid, "viewer")
        accepted = [r for r in view["readings"] if not r["pending"]]
        spike = accepted[-1]
        self.assertEqual(spike["force"], 5300.0)  # 原读数保留
        self.assertIsNotNone(spike["baseline"])
        self.assertIn("同温度段", spike["deduction"])
        self.assertIn("基线索力", spike["deduction"])

    def test_review_must_change_person(self):
        self._raise_warning()
        with self.assertRaises(PermissionDenied):
            self.service.manual_review(self.iid, {"anomaly": True}, OP,
                                       "bridge_engineer")

    def test_review_not_confirmed_keeps_status(self):
        self._raise_warning()
        review = self.service.manual_review(self.iid, {"anomaly": False, "note": "误报"},
                                            ENG, "bridge_engineer")
        self.assertFalse(review["anomaly"])
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"], "warning")
        with self.assertRaises(ConflictError):
            self.service.transition(self.iid, "restricted", 2, ENG,
                                    "bridge_engineer", {})

    def test_confirmed_review_requires_valid_notice(self):
        self._raise_warning()
        self.service.manual_review(self.iid, {"anomaly": True}, ENG,
                                   "bridge_engineer")
        # 通告缺失
        with self.assertRaises(ConflictError) as cm:
            self.service.transition(self.iid, "restricted", 2, ENG,
                                    "bridge_engineer", {})
        self.assertIn("通告缺失", str(cm.exception))
        # 引用不存在的通告
        with self.assertRaises(ConflictError):
            self.service.transition(self.iid, "restricted", 2, ENG,
                                    "bridge_engineer", {"notice_ref": "TN-X"})
        # 过期通告视为已失效
        self.service.add_record(self.iid, {
            "kind": "traffic_notice", "detail": "旧通告",
            "external_ref": "TN-OLD", "valid_until": t(0)},
            TA, "traffic_authority")
        with self.assertRaises(ConflictError) as cm:
            self.service.transition(self.iid, "restricted", 2, ENG,
                                    "bridge_engineer", {"notice_ref": "TN-OLD"})
        self.assertIn("有效期", str(cm.exception))
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"], "warning")

    def test_review_then_notice_allows_restriction_and_rescind_blocks(self):
        version = self._raise_warning()
        self.service.manual_review(self.iid, {"anomaly": True}, ENG,
                                   "bridge_engineer")
        notice = self.service.add_record(self.iid, {
            "kind": "traffic_notice", "detail": "限行通告",
            "external_ref": "TN-1"}, TA, "traffic_authority")
        updated = self.service.transition(self.iid, "restricted", version, ENG,
                                          "bridge_engineer", {"notice_ref": "TN-1"})
        self.assertEqual(updated["status"], "restricted")
        # 通告解除后，封闭被阻止，保留限行状态
        self.service.close_record(self.iid, notice["id"], TA, "traffic_authority")
        with self.assertRaises(ConflictError) as cm:
            self.service.transition(self.iid, "closed", updated["version"], TA,
                                    "traffic_authority", {"notice_ref": "TN-1"})
        self.assertIn("已解除", str(cm.exception))
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"],
                         "restricted")
        # 新有效通告后可以封闭
        self.service.add_record(self.iid, {
            "kind": "traffic_notice", "detail": "封闭通告",
            "external_ref": "TN-2"}, TA, "traffic_authority")
        closed = self.service.transition(self.iid, "closed", updated["version"], TA,
                                         "traffic_authority", {"notice_ref": "TN-2"})
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_only_sensor_operator_submits_readings(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_reading(self.iid, {
                "force": 5000.0, "temperature": 12.0, "read_at": t(9)},
                ENG, "bridge_engineer")

    def test_readings_rejected_for_generic_item(self):
        generic = self.service.create_item({
            "title": "普通告警", "description": "x", "severity": "normal",
            "threshold": 10, "external_ref": "G-1"}, OP, "sensor_operator")
        with self.assertRaises(ValidationError):
            self.service.submit_reading(generic["id"], {
                "force": 5000.0, "temperature": 12.0, "read_at": t(9)},
                OP, "sensor_operator")

    def _raise_warning(self):
        self.reading(5300.0, 12.0, 4)
        self.reading(5301.0, 12.1, 5)
        r = self.reading(5302.0, 12.2, 6)
        assert r["advice_changed"]
        return 2  # 初始version=1，自动升级后version=2


if __name__ == "__main__":
    unittest.main()
