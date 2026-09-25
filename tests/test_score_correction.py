import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402
import rules  # noqa: E402


class ScoreCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "correction.db")
        self.v1 = self.service.create_vendor("proc1", "procurement", "C-V-001", "启明科技", "vendor1")
        self.v2 = self.service.create_vendor("proc1", "procurement", "C-V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "C-T-001", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria,
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def _open_and_score(self):
        self.service.submit_bid("vendor1", "vendor", self.tender["id"], self.v1["id"], {"报价": 800000, "质量": 90}, 800000)
        self.service.submit_bid("vendor2", "vendor", self.tender["id"], self.v2["id"], {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.bid1, self.bid2 = opened["bids"][0]["id"], opened["bids"][1]["id"]
        self.service.evaluate_bid("expert", "evaluator", self.bid1, {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("expert", "evaluator", self.bid2, {"报价": 700000, "质量": 80})

    def _version(self):
        return self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["version"]

    def test_handoff_requires_supervisor_reason_and_owned_seat(self):
        self._open_and_score()
        with self.assertRaises(DomainError) as ctx:
            self.service.designate_handoff("expert", "evaluator", self.tender["id"], "expert", "backup", "离场")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "  ")
        with self.assertRaises(DomainError):
            self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "nobody", "backup", "离场")
        with self.assertRaises(DomainError):
            self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "expert", "离场")
        handoff = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场，账号收回")
        self.assertEqual("active", handoff["status"])
        self.assertEqual("sup1", handoff["designated_by"])

    def test_correction_without_handoff_or_wrong_successor_rejected(self):
        self._open_and_score()
        with self.assertRaises(DomainError) as ctx:
            self.service.correct_score("backup", "evaluator", 999, self.bid2, {"质量": 30})
        self.assertEqual(404, ctx.exception.status)
        self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场")
        with self.assertRaises(DomainError) as ctx:
            self.service.correct_score("other", "evaluator", 1, self.bid2, {"质量": 30})
        self.assertEqual(403, ctx.exception.status)

    def test_original_record_immutable_and_correction_keeps_old_new_time(self):
        self._open_and_score()
        self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场，账号收回")
        # 原账号再提交仍按“不能覆盖”拒绝，旧记录保持只读
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("expert", "evaluator", self.bid2, {"报价": 700000, "质量": 30})
        self.assertEqual(409, ctx.exception.status)
        result = self.service.correct_score("backup", "evaluator", 1, self.bid2, {"质量": 30})
        record = result["corrections"][0]
        self.assertEqual(80, record["old_raw_value"])
        self.assertEqual(30, record["new_raw_value"])
        self.assertGreater(record["new_score"], 0)
        self.assertEqual("专家离场，账号收回", record["handoff_reason"])
        self.assertTrue(record["processed_at"])
        self.assertEqual("sup1", record["authorized_by"])
        # 授权已消费：同一授权不能再次提交更正
        with self.assertRaises(DomainError) as consumed_ctx:
            self.service.correct_score("backup", "evaluator", 1, self.bid1, {"质量": 30})
        self.assertEqual(409, consumed_ctx.exception.status)
        # 监督员按当前持有人 backup 重新指定后才能继续更正
        self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "backup", "backup2", "接手人继续更换")
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])["scoring"]
        # 原始记录仍可查，有效值已变成接手人的更正
        seat = next(s for s in detail["effective_scores"] if s["bid_id"] == self.bid2 and s["criterion"] == "质量")
        self.assertEqual(80, seat["original_raw_value"])
        self.assertEqual(30, seat["raw_value"])
        self.assertEqual("expert", seat["original_evaluator"])
        self.assertEqual("backup", seat["effective_evaluator"])
        self.assertTrue(seat["corrected"])

    def test_correction_recalculates_ranking_changes_winner(self):
        self._open_and_score()
        before = self.service.get_tender("sup1", "supervisor", self.tender["id"])["scoring"]
        # 初始 bid1=96(价高但质量90)，bid2=92(价低但质量80)，bid1 领先
        self.assertEqual(self.bid1, before["ranking"][0]["bid_id"])
        self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场")
        up = self.service.correct_score("backup", "evaluator", 1, self.bid2, {"质量": 100})
        self.assertEqual(self.bid2, up["ranking"][0]["bid_id"])  # bid2=100 反超
        # 继续更正须由监督员按当前持有人 backup 再指定接手
        h2 = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "backup", "backup2", "接手人继续更换")
        down = self.service.correct_score("backup2", "evaluator", h2["id"], self.bid2, {"质量": 30})
        self.assertEqual(self.bid1, down["ranking"][0]["bid_id"])  # 回落
        history = self.service.get_tender("sup1", "supervisor", self.tender["id"])["scoring"]["ranking_history"]
        self.assertTrue(all(h["trigger_action"] == "score.corrected" for h in history))

    def test_chained_handoff_when_successor_also_leaves(self):
        self._open_and_score()
        h1 = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "首次离场")
        self.service.correct_score("backup", "evaluator", h1["id"], self.bid2, {"质量": 30})
        # 接手人再离场，按当前持有人 backup 继续交接
        h2 = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "backup", "third", "接手人调岗")
        result = self.service.correct_score("third", "evaluator", h2["id"], self.bid2, {"质量": 100})
        self.assertEqual(self.bid2, result["ranking"][0]["bid_id"])
        seat = result and self.service.get_tender("third", "evaluator", self.tender["id"])["scoring"]
        current = next(s for s in seat["effective_scores"] if s["bid_id"] == self.bid2 and s["criterion"] == "质量")
        self.assertEqual("third", current["effective_evaluator"])
        self.assertEqual("expert", current["original_evaluator"])
        self.assertEqual(2, len(seat["corrections"]))

    def test_incomplete_scoring_blocks_award(self):
        self.service.submit_bid("vendor1", "vendor", self.tender["id"], self.v1["id"], {"报价": 800000, "质量": 90}, 800000)
        self.service.submit_bid("vendor2", "vendor", self.tender["id"], self.v2["id"], {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        bid1 = opened["bids"][0]["id"]
        self.service.evaluate_bid("expert", "evaluator", bid1, {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], self._version())
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("尚未完成全部评分", str(ctx.exception))

    def test_open_complaint_blocks_award(self):
        self._open_and_score()
        self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分有异议")
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], self._version())
        self.assertIn("未处理投诉", str(ctx.exception))

    def test_award_snapshot_uses_effective_scores_and_locks_corrections(self):
        self._open_and_score()
        h1 = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场")
        self.service.correct_score("backup", "evaluator", h1["id"], self.bid2, {"质量": 30})
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], self._version())
        self.assertEqual(self.bid1, award["award"]["winner"]["bid_id"])
        self.assertEqual(1, len(award["award"]["corrections"]))
        detail = self.service.get_tender("auditor", "auditor", self.tender["id"])["scoring"]
        self.assertEqual("tender.awarded", detail["ranking_history"][-1]["trigger_action"])
        self.assertEqual(award["award"]["ranking"], detail["award_snapshot"]["ranking"])
        with self.assertRaises(DomainError):
            self.service.correct_score("backup", "evaluator", h1["id"], self.bid2, {"质量": 40})
        with self.assertRaises(DomainError):
            self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "已授标")

    def test_conflict_successor_cannot_correct(self):
        self._open_and_score()
        self.service.declare_conflict("backup", "evaluator", self.tender["id"], "backup", self.v2["id"], "曾任职该供应商")
        h1 = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场")
        with self.assertRaises(DomainError) as ctx:
            self.service.correct_score("backup", "evaluator", h1["id"], self.bid2, {"质量": 30})
        self.assertEqual(403, ctx.exception.status)

    def test_correction_value_range_validated(self):
        self._open_and_score()
        h1 = self.service.designate_handoff("sup1", "supervisor", self.tender["id"], "expert", "backup", "专家离场")
        with self.assertRaises(DomainError):
            self.service.correct_score("backup", "evaluator", h1["id"], self.bid2, {"质量": 130})

    def test_rules_module_pure_logic(self):
        criteria = [{"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
                    {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}]
        evals = [
            {"id": 1, "bid_id": 10, "evaluation_round": 1, "criterion": "报价", "evaluator": "e",
             "raw_value": 800000, "score": 100.0},
            {"id": 2, "bid_id": 10, "evaluation_round": 1, "criterion": "质量", "evaluator": "e",
             "raw_value": 80, "score": 80.0},
        ]
        corrections = [
            {"id": 1, "evaluation_id": 2, "successor_evaluator": "e2", "new_raw_value": 100, "new_score": 100.0},
            {"id": 2, "evaluation_id": 2, "successor_evaluator": "e3", "new_raw_value": 50, "new_score": 50.0},
        ]
        seats = rules.effective_seats(evals, corrections)
        quality = next(s for s in seats if s["criterion"] == "质量")
        self.assertEqual(50, quality["raw_value"])  # 后一条覆盖前一条
        self.assertEqual("e3", quality["effective_evaluator"])
        self.assertEqual(80, quality["original_raw_value"])  # 旧值保留
        ranking, incomplete = rules.rank_bids(
            [{"id": 10, "vendor_id": 1, "price": 800000}], evals, corrections, criteria
        )
        self.assertEqual([], incomplete)
        self.assertEqual(80.0, ranking[0]["score"])  # 100*0.6 + 50*0.4


if __name__ == "__main__":
    unittest.main()
