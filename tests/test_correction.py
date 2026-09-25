import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ScoreCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-101", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria,
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def open_round(self):
        self.service.submit_bid("vendor1", "vendor", self.tender["id"], self.vendor1["id"],
                                {"报价": 800000, "质量": 90}, 800000)
        self.service.submit_bid("vendor2", "vendor", self.tender["id"], self.vendor2["id"],
                                {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.tender = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        return opened["bids"][0], opened["bids"][1]

    def test_correction_keeps_original_and_recomputes_ranking_and_snapshot(self):
        bid1, bid2 = self.open_round()
        # 原离场专家 eval1 两家都评了；eval2 只评第二家
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})

        # 非监督员不能指定接手人
        with self.assertRaises(DomainError) as ctx:
            self.service.designate_handover("proc1", "procurement", self.tender["id"], "eval1", "eval3", "离职")
        self.assertEqual(403, ctx.exception.status)
        # 缺原因不能指定
        with self.assertRaises(DomainError):
            self.service.designate_handover("sup1", "supervisor", self.tender["id"], "eval1", "eval3", "  ")

        handover = self.service.designate_handover(
            "sup1", "supervisor", self.tender["id"], "eval1", "eval3", "专家离职，账号收回")
        self.assertEqual("eval3", handover["to_evaluator"])

        # 非接手人不能更正；接手人也不能改自己未获交接的专家分值（这里 eval3 只能改 eval1 的）
        with self.assertRaises(DomainError) as ctx2:
            self.service.correct_score("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx2.exception.status)
        # 必须一次性更正全部评分项
        with self.assertRaises(DomainError):
            self.service.correct_score("eval3", "evaluator", bid1["id"], {"报价": 600000})

        result = self.service.correct_score(
            "eval3", "evaluator", bid1["id"], {"报价": 600000, "质量": 100}, "复核台账后更正")
        rec = result["corrections"]
        self.assertEqual(2, len(rec))
        quality = next(r for r in rec if r["criterion"] == "质量")
        self.assertEqual(90.0, quality["old_raw_value"])
        self.assertEqual(100.0, quality["new_raw_value"])
        self.assertNotEqual(quality["old_score"], quality["new_score"])
        self.assertEqual("专家离职，账号收回", quality["handover_reason"])
        self.assertTrue(quality["created_at"])

        # 原记录仍可查看且未被修改
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        import sqlite3
        conn = sqlite3.connect(self.service.db_path)
        row = conn.execute(
            "SELECT raw_value FROM evaluations WHERE bid_id=? AND evaluator=? AND criterion='质量'",
            (bid1["id"], "eval1")).fetchone()
        conn.close()
        self.assertEqual(90.0, row[0])

        # 项目详情列出有效分值与更正记录
        eff = next(e for e in detail["effective_scores"] if e["bid_id"] == bid1["id"])
        q = next(c for c in eff["criteria"] if c["name"] == "质量")
        self.assertEqual("correction", q["source"])
        self.assertEqual(100.0, q["raw_value"])
        self.assertEqual("eval3", q["evaluator"])
        self.assertEqual(2, len(detail["score_corrections"]))
        self.assertEqual(1, len(detail["evaluator_handovers"]))

        # bid1 更正后排名应反超 bid2（原排名 bid1 领先，现更强）；实时排名按有效分
        self.assertEqual(bid1["id"], detail["live_ranking"][0]["bid_id"])

        # 同一评分项不能再次更正
        with self.assertRaises(DomainError) as ctx3:
            self.service.correct_score("eval3", "evaluator", bid1["id"], {"报价": 500000, "质量": 100})
        self.assertEqual(409, ctx3.exception.status)

        # 授标快照按有效分值，并追加到历史表
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual("effective_scores", award["award"]["basis"])
        self.assertEqual(bid1["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(2, award["award"]["correction_count"])
        self.assertEqual(2, len(award["award"]["ranking"][0]["criteria"]))
        final = self.service.get_tender("aud1", "auditor", self.tender["id"])
        self.assertEqual(1, len(final["ranking_snapshots"]))
        self.assertEqual(2, len(final["award_snapshot"]["ranking"][0]["criteria"]))
        # 授标后锁定，不能再更正
        with self.assertRaises(DomainError):
            self.service.correct_score("eval3", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})

    def test_award_blocked_when_scoring_incomplete(self):
        bid1, bid2 = self.open_round()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        # bid2 无人评分 -> 评分未完
        self.service.designate_handover("sup1", "supervisor", self.tender["id"], "eval9", "eval3", "备用交接")
        tender = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], tender["version"])
        self.assertIn("尚未完成全部评分", str(ctx.exception))

    def test_correction_without_handover_reason_is_rejected(self):
        bid1, _ = self.open_round()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        # 直接在库内构造缺原因的交接（防御性校验）
        import sqlite3
        self.service.designate_handover("sup1", "supervisor", self.tender["id"], "eval1", "eval3", "离职交接")
        conn = sqlite3.connect(self.service.db_path)
        conn.execute("UPDATE evaluator_handovers SET reason='' WHERE tender_id=? AND to_evaluator='eval3'",
                     (self.tender["id"],))
        conn.commit()
        conn.close()
        with self.assertRaises(DomainError) as ctx:
            self.service.correct_score("eval3", "evaluator", bid1["id"], {"报价": 600000, "质量": 100})
        self.assertIn("交接原因缺失", str(ctx.exception))

    def test_award_blocked_when_handover_reason_missing_and_complaint_open(self):
        bid1, bid2 = self.open_round()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        self.service.designate_handover("sup1", "supervisor", self.tender["id"], "eval9", "eval3", "备用交接")
        import sqlite3
        conn = sqlite3.connect(self.service.db_path)
        conn.execute("UPDATE evaluator_handovers SET reason='' WHERE tender_id=? AND to_evaluator='eval3'",
                     (self.tender["id"],))
        conn.commit()
        conn.close()
        tender = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], tender["version"])
        self.assertIn("交接原因缺失", str(ctx.exception))
        # 修复原因后，未处理投诉仍拦截
        conn = sqlite3.connect(self.service.db_path)
        conn.execute("UPDATE evaluator_handovers SET reason='补登记原因' WHERE tender_id=? AND to_evaluator='eval3'",
                     (self.tender["id"],))
        conn.commit()
        conn.close()
        self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "对更正流程有异议")
        with self.assertRaises(DomainError) as ctx2:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], tender["version"])
        self.assertIn("未处理投诉", str(ctx2.exception))

    def test_successor_who_already_scored_cannot_be_designated(self):
        bid1, _ = self.open_round()
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx:
            self.service.designate_handover("sup1", "supervisor", self.tender["id"], "eval1", "eval2", "离职")
        self.assertIn("已在本轮评分", str(ctx.exception))

    def test_reevaluation_round_correction_and_history_trace(self):
        bid1, bid2 = self.open_round()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        # 投诉成立进入第二轮重评：第一轮原记录保留
        complaint = self.service.submit_complaint("vendor2", "vendor", self.tender["id"], "评分有误")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "重新评审")
        tender = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual(2, tender["evaluation_round"])

        # 第二轮：离场专家 eval1 在新一轮完成评分后由接手人 eval3 更正
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 70})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 70})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 95})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 95})
        self.service.designate_handover("sup1", "supervisor", self.tender["id"], "eval1", "eval3", "离场交接")
        # 第一轮的更正记录不能在第二轮创建（evaluation 属于第一轮）
        self.service.correct_score("eval3", "evaluator", bid1["id"], {"报价": 800000, "质量": 95})

        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        # 更正记录带轮次，第一轮原始记录仍可追查
        cor = next(c for c in detail["score_corrections"] if c["criterion"] == "质量")
        self.assertEqual(2, cor["evaluation_round"])
        self.assertEqual(95.0, cor["new_raw_value"])
        self.assertEqual(70.0, cor["old_raw_value"])

        # 第二轮有效分值重算排名：bid1 质量被上调到95，与 bid2 质量95、价格更低 -> bid2 仍胜出
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(bid2["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(2, award["award"]["round"])
        final = self.service.get_tender("aud1", "auditor", self.tender["id"])
        self.assertEqual(1, len(final["ranking_snapshots"]))
        self.assertEqual(2, final["ranking_snapshots"][0]["evaluation_round"])


if __name__ == "__main__":
    unittest.main()
