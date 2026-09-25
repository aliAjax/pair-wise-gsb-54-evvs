"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional, Union

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ResourceConflict, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def _with_resource(self, records: Union[Dict[str, Any], List[Dict[str, Any]]]) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """为记录补充资源占用视图：船、计划时段、备缆消耗与船舶剩余容量。"""
        single = isinstance(records, dict)
        items = [records] if single else list(records)
        vessels = {vessel["name"]: vessel for vessel in self.repository.list_vessels()}
        enriched = []
        for record in items:
            payload = record.get("payload", {})
            vessel_name = payload.get("vessel_name")
            vessel = vessels.get(vessel_name) if vessel_name else None
            item = dict(record)
            item["resource"] = {
                "vessel_name": vessel_name,
                "planned_start": payload.get("planned_start"),
                "planned_end": payload.get("planned_end"),
                "spare_reserved_km": float(payload.get("spare_reserved_km", 0) or 0),
                "spare_consumed_km": float(payload.get("spare_consumed_km", 0) or 0),
                "restore_capacity_gbps": payload.get("restore_capacity_gbps"),
                "vessel_capacity_km": vessel["spare_capacity_km"] if vessel else None,
                "vessel_remaining_km": vessel["spare_remaining_km"] if vessel else None,
            }
            enriched.append(item)
        return enriched[0] if single else enriched

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_resource(self.repository.list_records(state=state, limit=limit))

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_resource(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        resource_plan = self.rules.resource_plan(record, action, new_payload)
        try:
            updated = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
                resource_plan=resource_plan,
            )
        except ResourceConflict as exc:
            self.audit.note(record_id, actor.user_id, "resource_blocked", {"action": action, "message": str(exc), "blockers": exc.blockers})
            raise
        return self._with_resource(updated)

    def register_vessel(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_vessel(actor.role):
            raise PermissionDenied("角色无权登记船舶")
        data = self.rules.validate_vessel(payload or {})
        return self.repository.create_vessel(data["name"], data["spare_capacity_km"], actor.user_id)

    def list_vessels(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_vessels()

    def get_vessel(self, actor: Actor, name: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        vessel = self.repository.get_vessel(text({"name": name}, "name"))
        vessel["allocations"] = self.repository.vessel_allocations(vessel["name"])
        return vessel

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
