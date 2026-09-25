"""Sealed public-procurement tendering and evaluation service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import rules

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "public_procurement.db"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("时间格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    deadline TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '[]',
                    evaluation_round INTEGER NOT NULL DEFAULT 1,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
                    awarded_bid_id INTEGER,
                    award_snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    representative TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sealed',
                    version INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    opened_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    score REAL NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(bid_id,evaluation_round,evaluator,criterion)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    declared_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,evaluator,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER REFERENCES vendors(id),
                    question TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    answered_by TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    complainant TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS score_handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluation_round INTEGER NOT NULL,
                    original_evaluator TEXT NOT NULL,
                    successor_evaluator TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    consumed INTEGER NOT NULL DEFAULT 0,
                    designated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS score_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
                    evaluation_round INTEGER NOT NULL,
                    criterion TEXT NOT NULL,
                    handoff_id INTEGER NOT NULL REFERENCES score_handoffs(id),
                    original_evaluator TEXT NOT NULL,
                    successor_evaluator TEXT NOT NULL,
                    handoff_reason TEXT NOT NULL,
                    old_raw_value REAL NOT NULL,
                    new_raw_value REAL NOT NULL,
                    old_score REAL NOT NULL,
                    new_score REAL NOT NULL,
                    corrected_by TEXT NOT NULL,
                    authorized_by TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ranking_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluation_round INTEGER NOT NULL,
                    trigger_action TEXT NOT NULL,
                    ranking TEXT NOT NULL,
                    incomplete_bids TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id,status);
                CREATE INDEX IF NOT EXISTS idx_eval_bid_round ON evaluations(bid_id,evaluation_round);
                CREATE INDEX IF NOT EXISTS idx_eval_tender_round ON evaluations(evaluation_round);
                CREATE INDEX IF NOT EXISTS idx_corr_round ON score_corrections(tender_id,evaluation_round);
                CREATE INDEX IF NOT EXISTS idx_corr_eval ON score_corrections(evaluation_id);
                CREATE INDEX IF NOT EXISTS idx_handoff_round ON score_handoffs(tender_id,evaluation_round,status);
                CREATE INDEX IF NOT EXISTS idx_rank_round ON ranking_snapshots(tender_id,evaluation_round,id);
                """
            )

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(tender_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (tender_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise DomainError("采购项目不存在", 404)
        return row

    def create_vendor(self, actor: str, role: str, vendor_no: str, name: str,
                      representative: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "创建供应商")
        if not vendor_no.strip() or not name.strip() or not representative.strip():
            raise DomainError("供应商编号、名称和代表不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                    (vendor_no.strip(), name.strip(), representative.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("供应商编号已存在", 409) from exc
            self._audit(conn, None, actor, "vendor.created", {"vendor_no": vendor_no.strip()})
            return dict(conn.execute("SELECT * FROM vendors WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_tender(self, actor: str, role: str, tender_no: str, title: str,
                      deadline: str, criteria: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "创建采购项目")
        parse_time(deadline)
        if not tender_no.strip() or not title.strip():
            raise DomainError("项目编号和标题不能为空")
        normalized_criteria = []
        total_weight = Decimal("0")
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("评分项格式无效")
            kind = item.get("kind", "direct")
            if kind not in {"direct", "cost"}:
                raise DomainError("评分项类型只支持 direct 或 cost")
            try:
                weight = Decimal(str(item["weight"]))
                max_value = Decimal(str(item.get("max_value", 100)))
            except (KeyError, InvalidOperation) as exc:
                raise DomainError("评分权重或上限无效") from exc
            if weight <= 0 or max_value <= 0:
                raise DomainError("评分权重和上限必须大于0")
            total_weight += weight
            normalized_criteria.append({"name": str(item["name"]).strip(), "kind": kind,
                                        "weight": float(weight), "max_value": float(max_value)})
        if not normalized_criteria or total_weight != 100:
            raise DomainError("评分项权重合计必须等于100")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO tenders(tender_no,title,description,deadline,criteria,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_no.strip(), title.strip(), description.strip(), parse_time(deadline).isoformat(timespec="seconds"),
                     json.dumps(normalized_criteria, ensure_ascii=False), actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "tender.created", {"tender_no": tender_no.strip()})
            return dict(self._tender(conn, cur.lastrowid))

    def publish_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "发布采购项目")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "draft":
                raise DomainError("只有草稿项目可以发布", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE tenders SET status='published',version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "tender.published", {"deadline": tender["deadline"]})
            return dict(self._tender(conn, tender_id))

    def submit_bid(self, actor: str, role: str, tender_id: int, vendor_id: int,
                   payload: dict[str, Any], price: float, expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "提交投标")
        if not isinstance(payload, dict):
            raise DomainError("投标内容必须是对象")
        try:
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise DomainError("报价必须是数值") from exc
        if price <= 0:
            raise DomainError("报价必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("当前项目不接受投标", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("投标截止时间已过", 409)
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            if not conn.execute("SELECT 1 FROM conflicts WHERE tender_id=? AND vendor_id=? AND evaluator=?", (tender_id, vendor_id, actor)).fetchone():
                pass
            existing = conn.execute("SELECT * FROM bids WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)).fetchone()
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            digest = canonical_hash(payload)
            if existing:
                if existing["status"] != "sealed":
                    raise DomainError("投标已撤回或已开标，不能修改", 409)
                if expected_version is None or existing["version"] != int(expected_version):
                    raise DomainError("投标已变化，请刷新后重试", 409)
                conn.execute(
                    "UPDATE bids SET payload=?,payload_hash=?,price=?,version=version+1,submitted_at=? WHERE id=? AND version=?",
                    (payload_text, digest, price, utcnow(), existing["id"], expected_version),
                )
                bid_id = existing["id"]
                action = "bid.updated"
            else:
                cur = conn.execute(
                    """INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, payload_text, digest, price, actor, utcnow()),
                )
                bid_id = cur.lastrowid
                action = "bid.submitted"
            self._audit(conn, tender_id, actor, action, {"bid_id": bid_id, "vendor_id": vendor_id, "hash": digest})
            bid = dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())
            bid["payload_hash"] = digest
            return bid

    def withdraw_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "撤回投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能撤回自己的投标", 403)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]) or bid["status"] != "sealed":
                raise DomainError("截止后不能撤回投标", 409)
            conn.execute("UPDATE bids SET status='withdrawn',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.withdrawn", {"bid_id": bid_id})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def open_bids(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "开标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("项目当前不能开标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) < parse_time(tender["deadline"]):
                raise DomainError("尚未到开标时间", 409)
            rows = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status='sealed' ORDER BY id", (tender_id,)).fetchall()
            opened = []
            now = utcnow()
            for row in rows:
                digest = canonical_hash(json.loads(row["payload"]))
                if digest != row["payload_hash"]:
                    raise DomainError("投标完整性校验失败: %s" % row["id"], 409)
                conn.execute("UPDATE bids SET status='opened',opened_at=?,version=version+1 WHERE id=?", (now, row["id"]))
                opened.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (row["id"],)).fetchone()))
            conn.execute("UPDATE tenders SET status='opened',version=version+1,updated_at=? WHERE id=?", (now, tender_id))
            self._audit(conn, tender_id, actor, "tender.opened", {"bid_count": len(opened)})
            return {"tender": dict(self._tender(conn, tender_id)), "bids": opened}

    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc
            self._audit(conn, tender_id, actor, "conflict.declared", {"evaluator": evaluator.strip(), "vendor_id": vendor_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与该供应商存在利益冲突", 403)
            criteria = json.loads(tender["criteria"])
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            created = []
            now = utcnow()
            for criterion in criteria:
                try:
                    raw = float(values[criterion["name"]])
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                try:
                    score = rules.score_for(criterion, raw)
                except rules.RuleError as exc:
                    raise DomainError(str(exc)) from exc
                existing = conn.execute(
                    """SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND evaluator=? AND criterion=?""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"]),
                ).fetchone()
                if existing:
                    raise DomainError("该评分项已提交，不能覆盖", 409)
                cur = conn.execute(
                    """INSERT INTO evaluations(bid_id,evaluation_round,evaluator,criterion,raw_value,score,comment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"], raw, score, comment.strip(), now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))
            self._audit(conn, tender["id"], actor, "bid.evaluated", {"bid_id": bid_id, "criteria": [item["criterion"] for item in created]})
            return {"bid_id": bid_id, "evaluator": actor, "round": tender["evaluation_round"], "evaluations": created}

    def disqualify_bid(self, actor: str, role: str, bid_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "废标")
        if not reason.strip():
            raise DomainError("废标理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("当前投标不能废标", 409)
            conn.execute("UPDATE bids SET status='disqualified',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified", {"bid_id": bid_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def ask_clarification(self, actor: str, role: str, tender_id: int, vendor_id: int, question: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交澄清")
        if not question.strip():
            raise DomainError("澄清问题不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            cur = conn.execute(
                "INSERT INTO clarifications(tender_id,vendor_id,question,created_at) VALUES(?,?,?,?)",
                (tender_id, vendor_id, question.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "clarification.asked", {"clarification_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (cur.lastrowid,)).fetchone())

    def answer_clarification(self, actor: str, role: str, clarification_id: int,
                             answer: str, publish: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "答复澄清")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if not row:
                raise DomainError("澄清不存在", 404)
            if row["status"] != "pending":
                raise DomainError("澄清已经处理", 409)
            status = "published" if publish else "answered"
            conn.execute(
                "UPDATE clarifications SET answer=?,status=?,answered_by=?,answered_at=? WHERE id=?",
                (answer.strip(), status, actor, utcnow(), clarification_id),
            )
            self._audit(conn, row["tender_id"], actor, "clarification.answered", {"clarification_id": clarification_id, "published": publish})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "complaint.submitted", {"complaint_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())

    def resolve_complaint(self, actor: str, role: str, complaint_id: int, decision: str,
                          resolution: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "处理投诉")
        if decision not in {"accepted", "rejected"} or not resolution.strip():
            raise DomainError("投诉决定或处理说明无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if not complaint:
                raise DomainError("投诉不存在", 404)
            if complaint["status"] != "open":
                raise DomainError("投诉已经处理", 409)
            conn.execute(
                "UPDATE complaints SET status=?,resolution=?,reviewed_by=?,resolved_at=? WHERE id=?",
                (decision, resolution.strip(), actor, utcnow(), complaint_id),
            )
            if decision == "accepted":
                tender = self._tender(conn, complaint["tender_id"])
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',evaluation_round=evaluation_round+1,evaluations_locked=0,version=version+1,updated_at=? WHERE id=?",
                    (utcnow(), tender["id"]),
                )
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved", {"complaint_id": complaint_id, "decision": decision})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())

    def _round_score_rows(self, conn: sqlite3.Connection, tender: sqlite3.Row) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """取当前评审轮次的原评分记录与更正记录。"""
        evaluations = [dict(r) for r in conn.execute(
            "SELECT * FROM evaluations WHERE evaluation_round=? AND bid_id IN (SELECT id FROM bids WHERE tender_id=?)",
            (tender["evaluation_round"], tender["id"]),
        ).fetchall()]
        corrections = [dict(r) for r in conn.execute(
            "SELECT * FROM score_corrections WHERE tender_id=? AND evaluation_round=?",
            (tender["id"], tender["evaluation_round"]),
        ).fetchall()]
        return evaluations, corrections

    def designate_handoff(self, actor: str, role: str, tender_id: int,
                          original_evaluator: str, successor_evaluator: str,
                          reason: str) -> dict[str, Any]:
        """监督员指定接手人并写明原因，作为评分更正的前置授权。"""
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "指定接手人")
        original_evaluator = (original_evaluator or "").strip()
        successor_evaluator = (successor_evaluator or "").strip()
        reason = (reason or "").strip()
        if not original_evaluator or not successor_evaluator:
            raise DomainError("原评审人与接手人不能为空")
        if original_evaluator == successor_evaluator:
            raise DomainError("接手人不能与原评审人相同")
        if not reason:
            raise DomainError("交接原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能办理评分交接", 409)
            prior_evaluations, prior_corrections = self._round_score_rows(conn, tender)
            prior_seats = rules.effective_seats(prior_evaluations, prior_corrections)
            active_handoffs = [dict(r) for r in conn.execute(
                "SELECT * FROM score_handoffs WHERE tender_id=? AND evaluation_round=? AND status='active' AND consumed=0",
                (tender_id, tender["evaluation_round"]),
            ).fetchall()]
            # 原评审人可以是原评分人，也可以是交接链上当前持有席位的接手人
            held = False
            for seat in prior_seats:
                if seat["effective_evaluator"] != original_evaluator:
                    continue
                already = any(
                    h["original_evaluator"] == original_evaluator
                    and h["successor_evaluator"] != original_evaluator
                    for h in active_handoffs
                )
                if already:
                    raise DomainError("该评审人持有的评分已有进行中的交接，不能重复指定", 409)
                held = True
            if not held:
                raise DomainError("原评审人在本轮没有可交接的有效评分")
            if any(h["successor_evaluator"] == successor_evaluator and
                   h["original_evaluator"] == original_evaluator for h in active_handoffs):
                raise DomainError("该接手授权已存在，不能重复指定", 409)
            now = utcnow()
            cur = conn.execute(
                """INSERT INTO score_handoffs(tender_id,evaluation_round,original_evaluator,successor_evaluator,reason,designated_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (tender_id, tender["evaluation_round"], original_evaluator, successor_evaluator, reason, actor, now),
            )
            self._audit(conn, tender_id, actor, "score.handoff_designated", {
                "handoff_id": cur.lastrowid, "evaluation_round": tender["evaluation_round"],
                "original_evaluator": original_evaluator, "successor_evaluator": successor_evaluator, "reason": reason,
            })
            return dict(conn.execute("SELECT * FROM score_handoffs WHERE id=?", (cur.lastrowid,)).fetchone())

    def correct_score(self, actor: str, role: str, handoff_id: int, bid_id: int,
                      values: dict[str, float]) -> dict[str, Any]:
        """由指定接手人凭有效交接授权提交更正分值；原记录只读，更正全部留痕。"""
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分更正")
        if not isinstance(values, dict) or not values:
            raise DomainError("更正评分必须提供至少一个评分项")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            handoff = conn.execute("SELECT * FROM score_handoffs WHERE id=?", (handoff_id,)).fetchone()
            if not handoff:
                raise DomainError("交接授权不存在", 404)
            if handoff["status"] != "active":
                raise DomainError("交接授权已失效，不能更正", 409)
            if handoff["consumed"]:
                raise DomainError("该交接授权已用于更正，再次更正须由监督员重新指定接手人", 409)
            if handoff["successor_evaluator"] != actor:
                raise DomainError("只有指定接手人可以提交更正", 403)
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["tender_id"] != handoff["tender_id"]:
                raise DomainError("投标与交接授权不属于同一项目", 409)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分更正", 409)
            if handoff["evaluation_round"] != tender["evaluation_round"]:
                raise DomainError("交接授权属于旧评审轮次，不能更正", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分更正", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("接手人与该供应商存在利益冲突", 403)
            criteria = json.loads(tender["criteria"])
            criteria_by_name = {c["name"]: c for c in criteria}
            unknown = [name for name in values if name not in criteria_by_name]
            if unknown:
                raise DomainError("未知评分项: " + ",".join(unknown))
            reason = (handoff["reason"] or "").strip()
            if not reason:
                raise DomainError("交接原因缺失，不能提交更正", 409)
            evaluations, corrections = self._round_score_rows(conn, tender)
            seats = {(s["bid_id"], s["criterion"]): s
                     for s in rules.effective_seats(evaluations, corrections)}
            created: list[dict[str, Any]] = []
            now = utcnow()
            for name, raw_input in values.items():
                seat = seats.get((bid_id, name))
                if seat is None:
                    raise DomainError("该投标没有此评分项的原记录，不能更正: " + name)
                # 授权链起点必须等于席位当前有效持有人：首次更正时为原评审人，
                # 再次交接后为上一任接手人
                if seat["effective_evaluator"] != handoff["original_evaluator"]:
                    raise DomainError(
                        "该评分项不由被交接评审人当前持有，需要按当前持有人重新指定接手人: " + name, 409
                    )
                try:
                    raw = float(raw_input)
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                try:
                    new_score = rules.score_for(criteria_by_name[name], raw)
                except rules.RuleError as exc:
                    raise DomainError(str(exc)) from exc
                cur = conn.execute(
                    """INSERT INTO score_corrections(tender_id,bid_id,evaluation_id,evaluation_round,criterion,handoff_id,
                                                     original_evaluator,successor_evaluator,handoff_reason,
                                                     old_raw_value,new_raw_value,old_score,new_score,
                                                     corrected_by,authorized_by,processed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tender["id"], bid_id, seat["evaluation_id"], tender["evaluation_round"], name, handoff_id,
                     handoff["original_evaluator"], actor, reason,
                     seat["raw_value"], raw, seat["score"], new_score,
                     actor, handoff["designated_by"], now),
                )
                row = dict(conn.execute("SELECT * FROM score_corrections WHERE id=?", (cur.lastrowid,)).fetchone())
                created.append(row)
                self._audit(conn, tender["id"], actor, "score.corrected", {
                    "correction_id": row["id"], "bid_id": bid_id, "criterion": name,
                    "handoff_id": handoff_id, "original_evaluator": handoff["original_evaluator"],
                    "old_raw_value": row["old_raw_value"], "new_raw_value": row["new_raw_value"],
                    "old_score": row["old_score"], "new_score": row["new_score"],
                    "reason": reason, "processed_at": now,
                })
            conn.execute("UPDATE score_handoffs SET consumed=1 WHERE id=?", (handoff_id,))
            # 更正后按有效分值重算本轮排名，并留一版快照供历史追查
            evaluations, corrections = self._round_score_rows(conn, tender)
            bid_rows = [dict(b) for b in conn.execute(
                "SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified')", (tender["id"],)
            ).fetchall()]
            ranking, incomplete = rules.rank_bids(bid_rows, evaluations, corrections, criteria)
            conn.execute(
                """INSERT INTO ranking_snapshots(tender_id,evaluation_round,trigger_action,ranking,incomplete_bids,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (tender["id"], tender["evaluation_round"], "score.corrected",
                 json.dumps(ranking, ensure_ascii=False), json.dumps(incomplete, ensure_ascii=False), actor, now),
            )
            conn.execute("UPDATE tenders SET version=version+1,updated_at=? WHERE id=?", (now, tender["id"]))
            return {"bid_id": bid_id, "corrected_by": actor, "round": tender["evaluation_round"],
                    "corrections": created, "ranking": ranking, "incomplete_bids": incomplete}

    def award_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "授标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能授标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            open_complaint = conn.execute("SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)).fetchone()["c"]
            if open_complaint:
                raise DomainError("存在未处理投诉，不能授标", 409)
            # 交接与更正必须写明原因，原因缺失一律拦截
            missing_handoff = conn.execute(
                "SELECT COUNT(*) AS c FROM score_handoffs WHERE tender_id=? AND evaluation_round=? AND NULLIF(TRIM(reason),'') IS NULL",
                (tender_id, tender["evaluation_round"]),
            ).fetchone()["c"]
            if missing_handoff:
                raise DomainError("存在交接原因缺失的评分交接，不能授标", 409)
            missing_correction = conn.execute(
                "SELECT COUNT(*) AS c FROM score_corrections WHERE tender_id=? AND evaluation_round=? AND NULLIF(TRIM(handoff_reason),'') IS NULL",
                (tender_id, tender["evaluation_round"]),
            ).fetchone()["c"]
            if missing_correction:
                raise DomainError("存在交接原因缺失的评分更正，不能授标", 409)
            bid_rows = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified')", (tender_id,)).fetchall()
            bids = [dict(b) for b in bid_rows]
            criteria = json.loads(tender["criteria"])
            evaluations, corrections = self._round_score_rows(conn, tender)
            ranking, incomplete = rules.rank_bids(bids, evaluations, corrections, criteria)
            if incomplete:
                raise DomainError("投标尚未完成全部评分: %s" % ",".join(str(i) for i in incomplete), 409)
            if not ranking:
                raise DomainError("没有可授标的有效投标", 409)
            winner = ranking[0]
            now = utcnow()
            correction_summary = [
                {"correction_id": item["id"], "bid_id": item["bid_id"], "criterion": item["criterion"],
                 "original_evaluator": item["original_evaluator"], "successor_evaluator": item["successor_evaluator"],
                 "old_score": item["old_score"], "new_score": item["new_score"], "reason": item["handoff_reason"],
                 "processed_at": item["processed_at"]}
                for item in corrections
            ]
            snapshot = {"tender_id": tender_id, "round": tender["evaluation_round"], "ranking": ranking,
                        "winner": winner, "corrections": correction_summary,
                        "awarded_by": actor, "awarded_at": now}
            conn.execute(
                "UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,evaluations_locked=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), now, tender_id, expected_version),
            )
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            conn.execute(
                """INSERT INTO ranking_snapshots(tender_id,evaluation_round,trigger_action,ranking,incomplete_bids,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (tender_id, tender["evaluation_round"], "tender.awarded",
                 json.dumps(ranking, ensure_ascii=False), json.dumps(incomplete, ensure_ascii=False), actor, now),
            )
            self._audit(conn, tender_id, actor, "tender.awarded", {"winner": winner, "ranking": ranking, "corrections": correction_summary})
            return {"tender": dict(self._tender(conn, tender_id)), "award": snapshot}

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = dict(self._tender(conn, tender_id))
            bids = []
            if role in {"procurement", "supervisor", "auditor"} and tender["status"] in {"opened", "reevaluation", "awarded"}:
                bids = [dict(r) for r in conn.execute("SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    "SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id WHERE b.tender_id=? AND b.submitted_by=?",
                    (tender_id, actor),
                ).fetchall():
                    item = dict(row)
                    item.pop("tender_status", None)
                    if tender["status"] not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
            else:
                bids = [dict(r) for r in conn.execute(
                    "SELECT id,tender_id,vendor_id,price,status,payload_hash,submitted_at,opened_at FROM bids WHERE tender_id=? ORDER BY id",
                    (tender_id,),
                ).fetchall()]
            clarifications = [dict(r) for r in conn.execute(
                "SELECT id,tender_id,vendor_id,question,answer,status,answered_at FROM clarifications WHERE tender_id=? AND status='published' ORDER BY id",
                (tender_id,),
            ).fetchall()]
            # 开标后任何角色都可查看有效分值与更正留痕；开标前不暴露评分信息
            scoring = None
            if tender["status"] in {"opened", "reevaluation", "awarded"}:
                round_no = tender["evaluation_round"]
                evaluations, corrections = self._round_score_rows(conn, tender)
                seats = rules.effective_seats(evaluations, corrections)
                bid_rows = [dict(b) for b in conn.execute(
                    "SELECT id,vendor_id,price,status FROM bids WHERE tender_id=? AND status IN ('opened','qualified','awarded') ORDER BY id",
                    (tender_id,),
                ).fetchall()]
                ranking, incomplete = rules.rank_bids(
                    bid_rows, evaluations, corrections, json.loads(tender["criteria"])
                )
                handoffs = [dict(r) for r in conn.execute(
                    "SELECT * FROM score_handoffs WHERE tender_id=? ORDER BY id", (tender_id,)
                ).fetchall()]
                ranking_history = []
                for row in conn.execute(
                    "SELECT id,evaluation_round,trigger_action,ranking,incomplete_bids,created_by,created_at FROM ranking_snapshots WHERE tender_id=? ORDER BY id",
                    (tender_id,),
                ).fetchall():
                    item = dict(row)
                    item["ranking"] = json.loads(item["ranking"])
                    item["incomplete_bids"] = json.loads(item["incomplete_bids"])
                    ranking_history.append(item)
                award_snapshot = tender["award_snapshot"]
                scoring = {
                    "round": round_no,
                    "effective_scores": seats,
                    "corrections": corrections,
                    "handoffs": handoffs,
                    "ranking": ranking,
                    "incomplete_bids": incomplete,
                    "ranking_history": ranking_history,
                    "award_snapshot": json.loads(award_snapshot) if award_snapshot else None,
                }
            return {"tender": tender, "bids": bids, "clarifications": clarifications, "scoring": scoring}

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            tenders = [dict(r) for r in conn.execute(
                "SELECT id,tender_no,title,description,status,deadline,evaluation_round,version,awarded_bid_id,created_at,updated_at FROM tenders ORDER BY id DESC"
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            if role in {"procurement", "supervisor", "auditor"}:
                bids = [dict(r) for r in conn.execute(
                    """SELECT b.id,b.tender_id,b.vendor_id,b.price,b.status,b.payload_hash,b.submitted_at,b.opened_at,
                              CASE WHEN t.status IN ('opened','reevaluation','awarded') THEN b.payload ELSE NULL END AS payload
                       FROM bids b JOIN tenders t ON t.id=b.tender_id ORDER BY b.id DESC LIMIT 200"""
                ).fetchall()]
                complaints = [dict(r) for r in conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 100").fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    """SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id
                       WHERE b.submitted_by=? ORDER BY b.id DESC LIMIT 100""",
                    (actor,),
                ).fetchall():
                    item = dict(row)
                    status = item.pop("tender_status")
                    if status not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
                complaints = [dict(r) for r in conn.execute(
                    "SELECT * FROM complaints WHERE complainant=? ORDER BY id DESC LIMIT 100", (actor,)
                ).fetchall()]
            else:
                bids, complaints = [], []
        return {"tenders": tenders, "bids": bids, "complaints": complaints, "timeline": timeline, "role": role}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM tenders").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        vendor = self.create_vendor("proc-demo", "procurement", "V-001", "启明科技", "vendor-demo")
        deadline = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")
        tender = self.create_tender(
            "proc-demo", "procurement", "TENDER-DEMO", "服务器采购", deadline,
            [{"name": "价格", "weight": 60, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}],
        )
        published = self.publish_tender("proc-demo", "procurement", tender["id"], tender["version"])
        self.submit_bid("vendor-demo", "vendor", tender["id"], vendor["id"], {"价格": 900000, "质量": 90}, 900000)
        return {"seeded": True, "tender_id": tender["id"], "vendor_id": vendor["id"], "published_version": published["version"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: ProcurementService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "public")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "public-procurement"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path.startswith("/api/tenders/"):
                self._send(200, self.service.get_tender(actor, role, int(path.split("/")[3])))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/vendors":
                result = self.service.create_vendor(actor, role, **data)
            elif path == "/api/tenders":
                result = self.service.create_tender(actor, role, **data)
            elif path == "/api/tenders/publish":
                result = self.service.publish_tender(actor, role, **data)
            elif path == "/api/bids":
                result = self.service.submit_bid(actor, role, **data)
            elif path == "/api/bids/withdraw":
                result = self.service.withdraw_bid(actor, role, **data)
            elif path == "/api/tenders/open":
                result = self.service.open_bids(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/evaluations":
                result = self.service.evaluate_bid(actor, role, **data)
            elif path == "/api/bids/disqualify":
                result = self.service.disqualify_bid(actor, role, **data)
            elif path == "/api/clarifications":
                result = self.service.ask_clarification(actor, role, **data)
            elif path == "/api/clarifications/answer":
                result = self.service.answer_clarification(actor, role, **data)
            elif path == "/api/complaints":
                result = self.service.submit_complaint(actor, role, **data)
            elif path == "/api/complaints/resolve":
                result = self.service.resolve_complaint(actor, role, **data)
            elif path == "/api/score-handoffs":
                result = self.service.designate_handoff(actor, role, **data)
            elif path == "/api/evaluations/correct":
                result = self.service.correct_score(actor, role, **data)
            elif path == "/api/tenders/award":
                result = self.service.award_tender(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: ProcurementService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Public procurement service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="公共采购密封投标服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8209)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = ProcurementService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
