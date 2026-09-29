import unittest

from src import cable
from src.cable import (BASELINE_COLD_START, BASELINE_SEGMENT_MEAN,
                       PENDING_MISSING_TEMP, PENDING_OFFLINE,
                       PENDING_TIME_INVERSION, compute_baseline,
                       detect_pending_reasons, next_streak, notice_is_effective,
                       parse_timestamp, temperature_bin_label)


def ts(hour):
    return parse_timestamp(f"2026-09-28T{hour:02d}:00:00+00:00")


class CableRulesTest(unittest.TestCase):
    def test_temperature_bins(self):
        self.assertEqual(temperature_bin_label(11.0), "10~15")
        self.assertEqual(temperature_bin_label(-3.0), "-5~0")
        self.assertEqual(temperature_bin_label(10.0), "10~15")

    def test_cold_start_baseline_seeds_first_reading(self):
        baseline, samples, method, corrected, excess = compute_baseline(
            12.0, 5000.0, [], upper_limit=50.0)
        self.assertEqual(method, BASELINE_COLD_START)
        self.assertEqual(samples, 0)
        self.assertEqual(corrected, 0.0)
        self.assertFalse(excess)

    def test_segment_baseline_removes_within_segment_drift(self):
        prior = [
            {"temp_bin": "10~15", "corrected_force": 0.0, "excess": False, "force": 5000.0},
            {"temp_bin": "10~15", "corrected_force": 2.0, "excess": False, "force": 5002.0},
        ]
        # 同温度段内索力缓变9kN，扣减约5001的基线后偏差远低于限值50
        baseline, samples, method, corrected, excess = compute_baseline(
            12.5, 5010.0, prior, upper_limit=50.0)
        self.assertEqual(method, BASELINE_SEGMENT_MEAN)
        self.assertEqual(samples, 2)
        self.assertAlmostEqual(baseline, 5001.0)
        self.assertAlmostEqual(corrected, 9.0)
        self.assertFalse(excess)
        # 同温度段内突跳100kN则仍判定超限
        _, _, _, _, excess_jump = compute_baseline(12.5, 5100.0, prior, upper_limit=50.0)
        self.assertTrue(excess_jump)

    def test_excess_readings_are_excluded_from_baseline(self):
        prior = [
            {"temp_bin": "10~15", "corrected_force": 0.0, "excess": False, "force": 5000.0},
            {"temp_bin": "10~15", "corrected_force": 300.0, "excess": True, "force": 5300.0},
        ]
        baseline, samples, _, _, _ = compute_baseline(
            12.0, 5010.0, prior, upper_limit=50.0)
        self.assertEqual(samples, 1)  # 超限跳点不污染基线
        self.assertEqual(baseline, 5000.0)

    def test_streak_requires_three_consecutive(self):
        prior = [
            {"temp_bin": "x", "corrected_force": 80.0, "excess": True, "streak": 1},
            {"temp_bin": "x", "corrected_force": 90.0, "excess": True, "streak": 2},
        ]
        self.assertEqual(next_streak(prior, True), 3)
        # 中间一笔恢复，连续计数清零
        self.assertEqual(next_streak(prior, False), 0)
        self.assertEqual(next_streak([], True), 1)

    def test_pending_reasons_are_collected(self):
        prior = [{"read_at_dt": ts(10)}]
        reasons = detect_pending_reasons(None, None, True, ts(9), prior)
        self.assertEqual(reasons, [PENDING_OFFLINE, PENDING_TIME_INVERSION,
                                   PENDING_MISSING_TEMP])

    def test_pending_reasons_offline_and_missing_temp(self):
        reasons = detect_pending_reasons(5000.0, None, True, ts(11), [])
        self.assertEqual(reasons, [PENDING_OFFLINE, PENDING_MISSING_TEMP])

    def test_time_inversion_equal_timestamp_is_pending(self):
        prior = [{"read_at_dt": ts(10)}]
        reasons = detect_pending_reasons(5000.0, 12.0, False, ts(10), prior)
        self.assertEqual(reasons, [PENDING_TIME_INVERSION])

    def test_missing_temperature_only(self):
        reasons = detect_pending_reasons(5000.0, None, False, ts(11), [])
        self.assertEqual(reasons, [PENDING_MISSING_TEMP])

    def test_healthy_reading_has_no_pending_reason(self):
        reasons = detect_pending_reasons(5000.0, 12.0, False, ts(11), [])
        self.assertEqual(reasons, [])

    def test_notice_effective(self):
        rec = {"status": "open", "valid_from": None, "valid_until": None}
        ok, _ = notice_is_effective(rec, now=ts(12))
        self.assertTrue(ok)

    def test_rescinded_notice_is_ineffective(self):
        rec = {"status": "closed", "valid_from": None, "valid_until": None}
        ok, reason = notice_is_effective(rec, now=ts(12))
        self.assertFalse(ok)
        self.assertIn("解除", reason)

    def test_expired_notice_is_ineffective(self):
        rec = {"status": "open", "valid_from": None,
               "valid_until": "2026-09-28T08:00:00+00:00"}
        ok, reason = notice_is_effective(rec, now=ts(12))
        self.assertFalse(ok)
        self.assertIn("有效期", reason)


if __name__ == "__main__":
    unittest.main()
