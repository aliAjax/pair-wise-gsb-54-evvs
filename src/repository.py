"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import ACTIVE_RESOURCE_STATES, Conflict, NotFound, ResourceConflict, ValidationError, parse_instant


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vessels (
                    name TEXT PRIMARY KEY,
                    spare_capacity_km REAL NOT NULL,
                    spare_reserved_km REAL NOT NULL DEFAULT 0,
                    spare_consumed_km REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], resource_plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if resource_plan:
                resource_result = self._apply_resource_plan(connection, resource_plan, now)
                if resource_result:
                    details = dict(details)
                    details["resource"] = resource_result
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def _apply_resource_plan(self, connection: sqlite3.Connection, plan: Dict[str, Any], now: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"ops": [], "vessels": {}}
        schedule = plan.get("schedule")
        if schedule:
            blocker = self._find_schedule_conflict(connection, schedule["vessel"], schedule["start"], schedule["end"], schedule.get("exclude_record_id"))
            if blocker is not None:
                raise ResourceConflict(
                    "船舶%s在%s至%s时段已被抢修%s（记录#%d，状态%s）占用" % (
                        schedule["vessel"], blocker["planned_start"], blocker["planned_end"],
                        blocker["reference"], blocker["record_id"], blocker["state"],
                    ),
                    blockers=[blocker],
                )
            result["schedule"] = {"vessel": schedule["vessel"], "start": schedule["start"], "end": schedule["end"]}
        for op in plan.get("ops", []):
            vessel_state = self._apply_vessel_op(connection, op, now)
            result["ops"].append(dict(op))
            result["vessels"].update(vessel_state)
        if not result["ops"] and not schedule:
            return {}
        return result

    def _apply_vessel_op(self, connection: sqlite3.Connection, op: Dict[str, Any], now: str) -> Dict[str, Any]:
        name = op["vessel"]
        row = connection.execute("SELECT * FROM vessels WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValidationError("船舶%s未登记" % name)
        capacity = float(row["spare_capacity_km"])
        reserved = float(row["spare_reserved_km"])
        consumed = float(row["spare_consumed_km"])
        kind = op["op"]
        if kind == "reserve":
            amount = round(float(op["amount_km"]), 3)
            self._ensure_spare(connection, name, capacity, reserved, consumed, amount)
            reserved = round(reserved + amount, 3)
        elif kind == "adjust":
            delta = round(float(op["delta_km"]), 3)
            if delta > 0:
                self._ensure_spare(connection, name, capacity, reserved, consumed, delta)
            reserved = max(0.0, round(reserved + delta, 3))
        elif kind == "consume":
            amount = round(float(op["amount_km"]), 3)
            reserved = max(0.0, round(reserved - amount, 3))
            consumed = round(consumed + amount, 3)
        elif kind == "release":
            amount = round(float(op["amount_km"]), 3)
            reserved = max(0.0, round(reserved - amount, 3))
        else:
            raise ValidationError("未知资源操作%s" % kind)
        connection.execute(
            "UPDATE vessels SET spare_reserved_km=?,spare_consumed_km=?,updated_at=? WHERE name=?",
            (reserved, consumed, now, name),
        )
        return {name: {"spare_capacity_km": capacity, "spare_reserved_km": reserved, "spare_consumed_km": consumed, "spare_remaining_km": round(capacity - reserved - consumed, 3)}}

    def _ensure_spare(self, connection: sqlite3.Connection, name: str, capacity: float, reserved: float, consumed: float, amount: float) -> None:
        remaining = round(capacity - reserved - consumed, 3)
        if remaining + 1e-9 >= amount:
            return
        holders = self._spare_holders(connection, name)
        if holders:
            occupied = "、".join("%s（记录#%d）预留%.2fkm" % (h["reference"], h["record_id"], h["spare_reserved_km"]) for h in holders)
        else:
            occupied = "无在修占用"
        raise ResourceConflict(
            "船舶%s剩余备缆%.2fkm，不足本次所需%.2fkm；在修占用：%s" % (name, remaining, amount, occupied),
            blockers=holders,
        )

    def _active_vessel_rows(self, connection: sqlite3.Connection, vessel: str) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_RESOURCE_STATES)
        rows = connection.execute(
            "SELECT id, reference, state, payload FROM records WHERE state IN (%s) ORDER BY id" % placeholders,
            ACTIVE_RESOURCE_STATES,
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("vessel_name") != vessel:
                continue
            result.append({"id": int(row["id"]), "reference": row["reference"], "state": row["state"], "payload": payload})
        return result

    def _find_schedule_conflict(self, connection: sqlite3.Connection, vessel: str, start: str, end: str, exclude_record_id: Optional[int]) -> Optional[Dict[str, Any]]:
        new_start = parse_instant(start)
        new_end = parse_instant(end)
        for item in self._active_vessel_rows(connection, vessel):
            if exclude_record_id is not None and item["id"] == int(exclude_record_id):
                continue
            held_start = item["payload"].get("planned_start")
            held_end = item["payload"].get("planned_end")
            if not held_start or not held_end:
                continue
            if new_start < parse_instant(held_end) and new_end > parse_instant(held_start):
                return {"record_id": item["id"], "reference": item["reference"], "state": item["state"], "planned_start": held_start, "planned_end": held_end}
        return None

    def _spare_holders(self, connection: sqlite3.Connection, vessel: str) -> List[Dict[str, Any]]:
        holders = []
        for item in self._active_vessel_rows(connection, vessel):
            reserved = float(item["payload"].get("spare_reserved_km", 0) or 0)
            if reserved <= 0:
                continue
            holders.append({"record_id": item["id"], "reference": item["reference"], "state": item["state"], "spare_reserved_km": reserved})
        return holders

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    @staticmethod
    def _vessel_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["spare_capacity_km"] = float(item["spare_capacity_km"])
        item["spare_reserved_km"] = float(item["spare_reserved_km"])
        item["spare_consumed_km"] = float(item["spare_consumed_km"])
        item["spare_remaining_km"] = round(item["spare_capacity_km"] - item["spare_reserved_km"] - item["spare_consumed_km"], 3)
        return item

    def create_vessel(self, name: str, spare_capacity_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO vessels(name,spare_capacity_km,spare_reserved_km,spare_consumed_km,created_by,created_at,updated_at) VALUES(?,?,0,0,?,?,?)",
                    (name, float(spare_capacity_km), actor_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("船舶已登记") from exc
        return self.get_vessel(name)

    def get_vessel(self, name: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM vessels WHERE name=?", (name,)).fetchone()
        if row is None:
            raise NotFound("船舶不存在")
        return self._vessel_row(row)

    def list_vessels(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM vessels ORDER BY name").fetchall()
        return [self._vessel_row(row) for row in rows]

    def vessel_allocations(self, name: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            items = self._active_vessel_rows(connection, name)
        return [
            {
                "record_id": item["id"],
                "reference": item["reference"],
                "state": item["state"],
                "planned_start": item["payload"].get("planned_start"),
                "planned_end": item["payload"].get("planned_end"),
                "spare_reserved_km": float(item["payload"].get("spare_reserved_km", 0) or 0),
                "spare_consumed_km": float(item["payload"].get("spare_consumed_km", 0) or 0),
            }
            for item in items
        ]

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
