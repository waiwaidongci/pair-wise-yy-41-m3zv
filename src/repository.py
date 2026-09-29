from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    valid_from TEXT,
                    valid_until TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_by TEXT,
                    closed_at TEXT,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cable_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    force REAL,
                    temperature REAL,
                    offline INTEGER NOT NULL DEFAULT 0,
                    read_at TEXT,
                    ingested_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('accepted','pending')),
                    pending_reason TEXT,
                    temp_bin TEXT,
                    baseline REAL,
                    baseline_samples INTEGER NOT NULL DEFAULT 0,
                    baseline_method TEXT,
                    corrected_force REAL,
                    excess INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    raw_force REAL,
                    created_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_cable_readings_item
                    ON cable_readings(item_id, id);
                CREATE TABLE IF NOT EXISTS cable_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    anomaly INTEGER NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_cable_reviews_item
                    ON cable_reviews(item_id, id);
            """)
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        """对旧库补齐后续版本新增的列（SQLite无法直接ADD COLUMN IF NOT EXISTS）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)").fetchall()}
        if "item_type" not in cols:
            self.conn.execute(
                "ALTER TABLE items ADD COLUMN item_type TEXT NOT NULL DEFAULT 'generic'")
        if "raised_by" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN raised_by TEXT")
        rec_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(records)").fetchall()}
        if "valid_from" not in rec_cols:
            self.conn.execute("ALTER TABLE records ADD COLUMN valid_from TEXT")
        if "valid_until" not in rec_cols:
            self.conn.execute("ALTER TABLE records ADD COLUMN valid_until TEXT")
        if "closed_by" not in rec_cols:
            self.conn.execute("ALTER TABLE records ADD COLUMN closed_by TEXT")
        if "closed_at" not in rec_cols:
            self.conn.execute("ALTER TABLE records ADD COLUMN closed_at TEXT")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, item_type: str = "generic") -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       item_type)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, item_type),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str, raised_by: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if raised_by is None:
                sql = ("UPDATE items SET status=?, version=version+1, updated_at=? "
                       "WHERE id=? AND version=?")
                params = (target, now, item_id, expected_version)
            else:
                sql = ("UPDATE items SET status=?, version=version+1, updated_at=?, "
                       "raised_by=? WHERE id=? AND version=?")
                params = (target, now, raised_by, item_id, expected_version)
            cur = self.conn.execute(sql, params)
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   valid_from: Optional[str] = None,
                   valid_until: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, valid_from, valid_until)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now,
                     valid_from, valid_until),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        return self.get_record(record_id)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return dict(row)

    def find_record_by_ref(self, item_id: int, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? AND external_ref=?",
                (item_id, external_ref),
            ).fetchone()
        return dict(row) if row else None

    def close_record(self, record_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE records SET status='closed', closed_by=?, closed_at=?
                   WHERE id=? AND status='open'""",
                (actor, now, record_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("记录不存在")
                raise ConflictError("记录已关闭，不能重复解除")
        return self.get_record(record_id)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 斜拉索读数与人工复核 ----
    @staticmethod
    def _reading(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["offline"] = bool(data["offline"])
        data["excess"] = bool(data["excess"])
        reasons = data.pop("pending_reason", None)
        data["pending_reasons"] = reasons.split(",") if reasons else []
        return data

    def add_reading(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO cable_readings(item_id, force, temperature, offline, read_at,
                   ingested_at, state, pending_reason, temp_bin, baseline, baseline_samples,
                   baseline_method, corrected_force, excess, streak, raw_force, created_by)
                   VALUES(:item_id,:force,:temperature,:offline,:read_at,:ingested_at,
                   :state,:pending_reason,:temp_bin,:baseline,:baseline_samples,
                   :baseline_method,:corrected_force,:excess,:streak,:raw_force,:created_by)""",
                {
                    "item_id": values["item_id"],
                    "force": values.get("force"),
                    "temperature": values.get("temperature"),
                    "offline": 1 if values.get("offline") else 0,
                    "read_at": values.get("read_at"),
                    "ingested_at": values["ingested_at"],
                    "state": values["state"],
                    "pending_reason": ",".join(values.get("pending_reasons", [])) or None,
                    "temp_bin": values.get("temp_bin"),
                    "baseline": values.get("baseline"),
                    "baseline_samples": values.get("baseline_samples", 0),
                    "baseline_method": values.get("baseline_method"),
                    "corrected_force": values.get("corrected_force"),
                    "excess": 1 if values.get("excess") else 0,
                    "streak": values.get("streak", 0),
                    "raw_force": values.get("raw_force"),
                    "created_by": values["created_by"],
                },
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM cable_readings WHERE id=?", (reading_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return self._reading(row)

    def list_readings(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cable_readings WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._reading(r) for r in rows]

    def accepted_readings(self, item_id: int) -> List[Dict[str, Any]]:
        """已采纳读数（按提交顺序），供温度基线与连续计数使用。"""
        from .cable import parse_timestamp
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cable_readings WHERE item_id=? AND state='accepted' ORDER BY id",
                (item_id,),
            ).fetchall()
        result = [self._reading(r) for r in rows]
        for r in result:
            r["read_at_dt"] = parse_timestamp(r["read_at"])
        return result

    def pending_reading_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM cable_readings WHERE item_id=? AND state='pending'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def add_review(self, item_id: int, reviewer: str, anomaly: bool,
                   note: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO cable_reviews(item_id, reviewer, anomaly, note, created_at)
                   VALUES(?,?,?,?,?)""",
                (item_id, reviewer, 1 if anomaly else 0, note, now),
            )
            review_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM cable_reviews WHERE id=?", (review_id,)
            ).fetchone()
        data = dict(row)
        data["anomaly"] = bool(data["anomaly"])
        return data

    def list_reviews(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cable_reviews WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["anomaly"] = bool(data["anomaly"])
            result.append(data)
        return result

    def confirmed_review(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM cable_reviews WHERE item_id=? AND anomaly=1 ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["anomaly"] = True
        return data

    def close(self) -> None:
        with self._lock:
            self.conn.close()
