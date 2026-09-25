"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError
from .rules import find_vessel_conflict, parse_time_value


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
                    spare_cable_km REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel_name TEXT NOT NULL REFERENCES vessels(name),
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    planned_start TEXT NOT NULL,
                    planned_end TEXT NOT NULL,
                    reserved_km REAL NOT NULL,
                    consumed_km REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reservations_vessel ON reservations(vessel_name, status);
                CREATE INDEX IF NOT EXISTS idx_reservations_record ON reservations(record_id, status);
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

    def create_vessel(self, name: str, spare_cable_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO vessels(name,spare_cable_km,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (name, spare_cable_km, actor_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("船舶已存在") from exc
        return self.get_vessel(name)

    def get_vessel(self, name: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM vessels WHERE name=?", (name,)).fetchone()
            if row is None:
                raise NotFound("船舶不存在")
            reservations = connection.execute("SELECT * FROM reservations WHERE vessel_name=? ORDER BY id DESC", (name,)).fetchall()
        item = dict(row)
        item["reservations"] = [dict(reservation) for reservation in reservations]
        return item

    def list_vessels(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            vessels = connection.execute("SELECT * FROM vessels ORDER BY name").fetchall()
            active = connection.execute("SELECT * FROM reservations WHERE status='active' ORDER BY id").fetchall()
        by_vessel: Dict[str, List[Dict[str, Any]]] = {}
        for row in active:
            by_vessel.setdefault(row["vessel_name"], []).append(dict(row))
        result = []
        for row in vessels:
            item = dict(row)
            item["active_reservations"] = by_vessel.get(item["name"], [])
            result.append(item)
        return result

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], resource_op: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            version = int(expected_version) + 1
            if resource_op is not None:
                resource_result = self._apply_resource_op(connection, record_id, resource_op)
                details = dict(details)
                details["resource"] = resource_result
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

    def _release_record_reservations(self, connection: sqlite3.Connection, record_id: int) -> float:
        """释放记录当前全部占用，未消耗备缆归还船舶，返回归还总量。"""
        rows = connection.execute("SELECT * FROM reservations WHERE record_id=? AND status='active'", (record_id,)).fetchall()
        now = _now()
        returned = 0.0
        for row in rows:
            give_back = round(float(row["reserved_km"]) - float(row["consumed_km"]), 4)
            if give_back > 0:
                connection.execute(
                    "UPDATE vessels SET spare_cable_km=round(spare_cable_km+?,4), updated_at=? WHERE name=?",
                    (give_back, now, row["vessel_name"]),
                )
            connection.execute("UPDATE reservations SET status='released', updated_at=? WHERE id=?", (now, row["id"]))
            returned += give_back
        return round(returned, 4)

    def _apply_resource_op(self, connection: sqlite3.Connection, record_id: int, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op["kind"]
        now = _now()
        if kind == "reserve":
            released_km = self._release_record_reservations(connection, record_id)
            vessel = connection.execute("SELECT * FROM vessels WHERE name=?", (op["vessel_name"],)).fetchone()
            if vessel is None:
                raise NotFound("船舶%s未登记" % op["vessel_name"])
            rows = connection.execute("SELECT * FROM reservations WHERE vessel_name=? AND status='active'", (op["vessel_name"],)).fetchall()
            holder = find_vessel_conflict(
                [dict(row) for row in rows],
                parse_time_value(op["planned_start"]),
                parse_time_value(op["planned_end"]),
                exclude_record_id=record_id,
            )
            if holder is not None:
                raise Conflict("船舶%s在该计划时段已被记录#%s的抢修占用" % (op["vessel_name"], holder["record_id"]))
            remaining = round(float(vessel["spare_cable_km"]) - float(op["required_km"]), 4)
            if remaining < 0:
                raise Conflict("船舶%s剩余备缆%.2fkm，不足本次所需%.2fkm" % (op["vessel_name"], float(vessel["spare_cable_km"]), float(op["required_km"])))
            connection.execute("UPDATE vessels SET spare_cable_km=?, updated_at=? WHERE name=?", (remaining, now, op["vessel_name"]))
            cursor = connection.execute(
                "INSERT INTO reservations(vessel_name,record_id,planned_start,planned_end,reserved_km,consumed_km,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (op["vessel_name"], record_id, op["planned_start"], op["planned_end"], float(op["required_km"]), 0.0, "active", now, now),
            )
            return {"kind": kind, "vessel_name": op["vessel_name"], "reservation_id": int(cursor.lastrowid), "reserved_km": float(op["required_km"]), "released_km": released_km, "vessel_remaining_km": remaining}
        if kind == "adjust":
            row = connection.execute("SELECT * FROM reservations WHERE record_id=? AND status='active'", (record_id,)).fetchone()
            if row is None:
                raise Conflict("该记录没有进行中的资源占用")
            diff = round(float(op["actual_km"]) - float(row["reserved_km"]), 4)
            vessel = connection.execute("SELECT * FROM vessels WHERE name=?", (row["vessel_name"],)).fetchone()
            remaining = float(vessel["spare_cable_km"])
            if diff > 0:
                if remaining < diff:
                    raise Conflict("船舶%s剩余备缆%.2fkm，无法补足实际装载差额%.2fkm" % (row["vessel_name"], remaining, diff))
                remaining = round(remaining - diff, 4)
                connection.execute("UPDATE vessels SET spare_cable_km=?, updated_at=? WHERE name=?", (remaining, now, row["vessel_name"]))
            elif diff < 0:
                remaining = round(remaining - diff, 4)
                connection.execute("UPDATE vessels SET spare_cable_km=?, updated_at=? WHERE name=?", (remaining, now, row["vessel_name"]))
            connection.execute("UPDATE reservations SET reserved_km=?, updated_at=? WHERE id=?", (float(op["actual_km"]), now, row["id"]))
            return {"kind": kind, "vessel_name": row["vessel_name"], "reservation_id": int(row["id"]), "reserved_km": float(op["actual_km"]), "adjustment_km": diff, "vessel_remaining_km": remaining}
        if kind == "consume":
            row = connection.execute("SELECT * FROM reservations WHERE record_id=? AND status='active'", (record_id,)).fetchone()
            if row is None:
                raise Conflict("该记录没有进行中的资源占用")
            if float(op["used_km"]) > float(row["reserved_km"]) + 1e-6:
                raise Conflict("接续消耗%.2fkm超过船上备缆%.2fkm" % (float(op["used_km"]), float(row["reserved_km"])))
            connection.execute("UPDATE reservations SET consumed_km=?, updated_at=? WHERE id=?", (float(op["used_km"]), now, row["id"]))
            return {"kind": kind, "vessel_name": row["vessel_name"], "reservation_id": int(row["id"]), "consumed_km": float(op["used_km"])}
        if kind == "release":
            return {"kind": kind, "returned_km": self._release_record_reservations(connection, record_id)}
        raise ValidationError("未知资源操作")

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

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
