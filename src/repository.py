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
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    tmin REAL NOT NULL,
                    tmax REAL,
                    baseline_force REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, tmin)
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    force REAL,
                    temperature REAL,
                    measured_at TEXT NOT NULL,
                    measured_at_raw TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('accepted','pending')),
                    pending_reason TEXT,
                    baseline_id INTEGER REFERENCES baselines(id) ON DELETE SET NULL,
                    band_tmin REAL,
                    band_tmax REAL,
                    baseline_force REAL,
                    force_adjusted REAL,
                    over_limit INTEGER NOT NULL DEFAULT 0,
                    alarm_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_readings_item
                    ON readings(item_id, status, measured_at, id);
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','confirmed','rejected')),
                    evidence TEXT NOT NULL,
                    record_id INTEGER,
                    raised_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
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
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

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

    # ===== 温度段基线 =====
    def add_baseline(self, item_id: int, tmin: float, tmax: Optional[float],
                     baseline_force: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO baselines(item_id, tmin, tmax, baseline_force,
                       created_by, created_at) VALUES(?,?,?,?,?,?)""",
                    (item_id, tmin, tmax, baseline_force, actor, now),
                )
                baseline_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该温度段起点基线已存在") from exc
        return self.get_baseline(baseline_id)

    def get_baseline(self, baseline_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM baselines WHERE id=?", (baseline_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("基线不存在")
        return dict(row)

    def list_baselines(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM baselines WHERE item_id=? ORDER BY tmin", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ===== 索力读数 =====
    def insert_reading(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO readings(item_id, force, temperature, measured_at,
                   measured_at_raw, status, pending_reason, baseline_id, band_tmin,
                   band_tmax, baseline_force, force_adjusted, over_limit, alarm_id,
                   created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["item_id"], data.get("force"), data.get("temperature"),
                 data["measured_at"], data["measured_at_raw"], data["status"],
                 data.get("pending_reason"), data.get("baseline_id"),
                 data.get("band_tmin"), data.get("band_tmax"),
                 data.get("baseline_force"), data.get("force_adjusted"),
                 1 if data.get("over_limit") else 0, data.get("alarm_id"),
                 data["created_by"], utc_now()),
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=?", (reading_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def last_accepted_reading(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM readings WHERE item_id=? AND status='accepted'
                   ORDER BY measured_at DESC, id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def recent_accepted_readings(self, item_id: int, limit: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM readings WHERE item_id=? AND status='accepted'
                   AND alarm_id IS NULL
                   ORDER BY measured_at DESC, id DESC LIMIT ?""",
                (item_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def list_readings(self, item_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        sql = "SELECT * FROM readings WHERE item_id=?"
        params: tuple = (item_id,)
        if status:
            sql += " AND status=?"
            params = (item_id, status)
        sql += " ORDER BY measured_at, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ===== 严重告警与自动升级（同一事务） =====
    def raise_severe_alarm(self, item_id: int, expected_version: int,
                           evidence: dict, reading_ids: list, actor: str,
                           record_id: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET severity='critical', version=version+1, updated_at=?
                   WHERE id=? AND status='normal' AND version=?""",
                (now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("状态已变化或版本冲突，请刷新后重试")
            cur = self.conn.execute(
                """INSERT INTO alarms(item_id, status, evidence, record_id,
                   raised_by, created_at) VALUES(?,?,?,?,?,?)""",
                (item_id, "open", json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                 record_id, actor, now),
            )
            alarm_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE readings SET alarm_id=? WHERE id IN (%s)"
                % ",".join("?" * len(reading_ids)),
                [alarm_id, *reading_ids],
            )
        with self._lock:
            row = self.conn.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        return self._alarm(row)

    def get_alarm(self, alarm_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM alarms WHERE id=?", (alarm_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("告警不存在")
        return self._alarm(row)

    def open_alarm_for_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM alarms WHERE item_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return self._alarm(row) if row else None

    @staticmethod
    def _alarm(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["evidence"] = json.loads(item["evidence"])
        return item

    def review_alarm(self, alarm_id: int, decision: str, reviewer: str, note: str,
                     record_id: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE alarms SET status=?, reviewed_by=?, review_note=?,
                   reviewed_at=?, record_id=COALESCE(?, record_id)
                   WHERE id=? AND status='open'""",
                (decision, reviewer, note, now, record_id, alarm_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM alarms WHERE id=?", (alarm_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("告警不存在")
                raise ConflictError("告警已复核，不能重复确认")
        return self.get_alarm(alarm_id)

    def open_traffic_notice_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM records
                   WHERE item_id=? AND kind=? AND status='open'
                     AND external_ref IS NOT NULL""",
                (item_id, "traffic_notice"),
            ).fetchone()
        return int(row["n"])

    def close_record(self, record_id: int, actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=? AND status='open'",
                (record_id,),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("记录不存在")
                raise ConflictError("记录已关闭")
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return dict(row)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
