"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, parse_instant, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
VESSEL_MANAGER_ROLES = {'repair_manager'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'reassign': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'reassign': {'approved': 'approved', 'mobilized': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        all_roles.update(VESSEL_MANAGER_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_manage_vessel(self, role: str) -> bool:
        return role == "admin" or role in VESSEL_MANAGER_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def validate_vessel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        name = text(p, "name")
        if len(name) > 64:
            raise ValidationError("船舶名称过长")
        capacity = number(p, "spare_capacity_km", 0)
        return {"name": name, "spare_capacity_km": round(capacity, 3)}

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def _planned_window(self, data: Dict[str, Any], p: Dict[str, Any]) -> Tuple[str, str]:
        start = parse_instant(text(data, "planned_start"))
        end = parse_instant(text(data, "planned_end"))
        if end <= start:
            raise ValidationError("计划结束时间必须晚于开始时间")
        hours = (end - start).total_seconds() / 3600.0
        if hours < float(p.get("estimated_repair_hours", 0)):
            raise ValidationError("计划时段不足以覆盖预计抢修时长%.2f小时" % float(p["estimated_repair_hours"]))
        return start.isoformat(), end.isoformat()

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            changes["vessel_name"] = text(data, "vessel_name")
            start, end = self._planned_window(data, p)
            changes["planned_start"] = start
            changes["planned_end"] = end
            changes["spare_reserved_km"] = round(float(p["required_spare_km"]), 3)
            changes["spare_consumed_km"] = 0.0
            summary = "抢修方案已批准"
        elif action == "reassign":
            changes["vessel_name"] = text(data, "vessel_name")
            start, end = self._planned_window(data, p)
            changes["planned_start"] = start
            changes["planned_end"] = end
            changes["spare_reserved_km"] = round(float(p["required_spare_km"]), 3)
            p.pop("weather_window_hours", None)
            p.pop("available_spare_km", None)
            summary = "已改派至%s" % changes["vessel_name"]
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            vessel_assigned = p.get("vessel_name")
            vessel_in_data = data.get("vessel_name")
            if vessel_assigned and isinstance(vessel_in_data, str) and vessel_in_data.strip() and vessel_in_data.strip() != vessel_assigned:
                raise ValidationError("动员船舶与审批选定船舶不一致")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["available_spare_km"] = round(float(data["available_spare_km"]), 3)
            changes["spare_reserved_km"] = round(float(data["available_spare_km"]), 3)
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            used = float(data.get("spare_used_km", 0))
            if used < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            if used > float(p.get("spare_reserved_km", 0)) + 1e-6:
                raise ValidationError("实际用量超过船上备缆")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = used
            changes["spare_reserved_km"] = round(float(p.get("spare_reserved_km", 0)) - used, 3)
            changes["spare_consumed_km"] = round(float(p.get("spare_consumed_km", 0)) + used, 3)
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            changes["spare_reserved_km"] = 0.0
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            changes["spare_reserved_km"] = 0.0
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def resource_plan(self, record: Dict[str, Any], action: str, new_payload: Dict[str, Any]) -> Dict[str, Any]:
        """本次动作需要在同一事务内执行的船舶资源操作（先释放后占用）。"""
        old = record["payload"]
        plan: Dict[str, Any] = {"schedule": None, "ops": []}
        if action in ("approve", "reassign"):
            if action == "reassign":
                old_vessel = old.get("vessel_name")
                old_reserved = float(old.get("spare_reserved_km", 0) or 0)
                if old_vessel and old_reserved > 0:
                    plan["ops"].append({"op": "release", "vessel": old_vessel, "amount_km": round(old_reserved, 3)})
            vessel = new_payload.get("vessel_name")
            plan["schedule"] = {"vessel": vessel, "start": new_payload.get("planned_start"), "end": new_payload.get("planned_end"), "exclude_record_id": record.get("id")}
            plan["ops"].append({"op": "reserve", "vessel": vessel, "amount_km": round(float(new_payload.get("spare_reserved_km", 0)), 3)})
        elif action == "mobilize":
            vessel = old.get("vessel_name")
            delta = round(float(new_payload.get("available_spare_km", 0)) - float(old.get("spare_reserved_km", 0) or 0), 3)
            if vessel and abs(delta) > 1e-9:
                plan["ops"].append({"op": "adjust", "vessel": vessel, "delta_km": delta})
        elif action == "splice":
            vessel = old.get("vessel_name")
            used = float(new_payload.get("spare_used_km", 0) or 0)
            if vessel and used > 0:
                plan["ops"].append({"op": "consume", "vessel": vessel, "amount_km": round(used, 3)})
        elif action in ("restore", "cancel"):
            vessel = old.get("vessel_name")
            reserved = float(old.get("spare_reserved_km", 0) or 0)
            if vessel and reserved > 0:
                plan["ops"].append({"op": "release", "vessel": vessel, "amount_km": round(reserved, 3)})
        return plan
