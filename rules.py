"""评分规则（纯函数）：原始录入值换算、有效分值归并与排名计算。

本模块不访问数据库、不依赖第三方库，评分规则单独维护，
服务层（app.py）与页面只引用这里的计算结果，便于核对与测试。

有效分值的归并规则：
- evaluations 中的原评分记录不可变，每条记录代表一个评审“席位”的一票；
- score_corrections 按处理顺序串成交接链，后一条更正覆盖前一条的有效值，
  原记录的旧值仍保留在 evaluations 与 score_corrections 中可追查；
- 每个评分项的最终得分取该席位当前有效分值，多席位时按项取平均，
  再按评分项权重加权求和。
"""
from __future__ import annotations

from typing import Any


class RuleError(ValueError):
    """评分规则校验失败。"""


def score_for(criterion: dict[str, Any], raw: float) -> float:
    """按评分项规则把原始录入值换算为 0-100 的得分。"""
    if raw < 0 or raw > criterion["max_value"]:
        raise RuleError("评分值超出范围: " + criterion["name"])
    if criterion["kind"] == "direct":
        return raw / criterion["max_value"] * 100
    benchmark = criterion["max_value"]
    return min(100.0, benchmark / raw * 100) if raw > 0 else 0.0


def effective_seats(evaluations: list[dict[str, Any]],
                    corrections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把原评分记录与更正记录归并为每个评审席位的有效分值。

    席位的原记录始终保留；effective_evaluator/raw_value/score 表示
    经过交接链更正后的当前有效值。
    """
    seats: dict[int, dict[str, Any]] = {}
    for row in evaluations:
        seats[row["id"]] = {
            "evaluation_id": row["id"],
            "bid_id": row["bid_id"],
            "evaluation_round": row["evaluation_round"],
            "criterion": row["criterion"],
            "original_evaluator": row["evaluator"],
            "effective_evaluator": row["evaluator"],
            "original_raw_value": row["raw_value"],
            "raw_value": row["raw_value"],
            "original_score": row["score"],
            "score": row["score"],
            "corrected": False,
            "correction_id": None,
        }
    for item in sorted(corrections, key=lambda c: c["id"]):
        seat = seats.get(item["evaluation_id"])
        if seat is None:
            continue
        seat["raw_value"] = item["new_raw_value"]
        seat["score"] = item["new_score"]
        seat["effective_evaluator"] = item["successor_evaluator"]
        seat["corrected"] = True
        seat["correction_id"] = item["id"]
    return list(seats.values())


def rank_bids(bids: list[dict[str, Any]], evaluations: list[dict[str, Any]],
              corrections: list[dict[str, Any]],
              criteria: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """按有效分值计算排名。

    返回 ``(ranking, incomplete_bids)``：评分未完成的投标不进入排名，
    其 bid_id 放入 incomplete_bids，由调用方决定提示或阻止授标。
    """
    seats = effective_seats(evaluations, corrections)
    expected = {c["name"] for c in criteria}
    votes_by_bid: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for seat in seats:
        votes_by_bid.setdefault(seat["bid_id"], {}).setdefault(seat["criterion"], []).append(seat)
    ranking: list[dict[str, Any]] = []
    incomplete: list[int] = []
    for bid in bids:
        per_criterion = votes_by_bid.get(bid["id"], {})
        if set(per_criterion) != expected:
            incomplete.append(bid["id"])
            continue
        weighted = 0.0
        for criterion in criteria:
            vote_scores = [seat["score"] for seat in per_criterion[criterion["name"]]]
            average = sum(vote_scores) / len(vote_scores)
            weighted += average * criterion["weight"] / 100
        ranking.append({
            "bid_id": bid["id"],
            "vendor_id": bid["vendor_id"],
            "price": bid["price"],
            "score": round(weighted, 2),
        })
    ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
    return ranking, sorted(incomplete)
