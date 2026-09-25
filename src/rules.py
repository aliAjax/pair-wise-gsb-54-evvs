"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
VESSEL_MANAGE_ROLES = {'repair_manager'}
TRANSITIONS = {'approve': {'detected': 'approved', 'approved': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}


def parse_time_value(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("时间格式无效：%s" % value) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_vessel_conflict(reservations: Iterable[Dict[str, Any]], planned_start: datetime, planned_end: datetime, exclude_record_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """在进行中的占用里找计划时段重叠者，返回占住该时段的占用记录。"""
    for item in reservations:
        if exclude_record_id is not None and int(item["record_id"]) == int(exclude_record_id):
            continue
        start = parse_time_value(item["planned_start"])
        end = parse_time_value(item["planned_end"])
        if planned_start < end and planned_end > start:
            return item
    return None


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_manage_vessels(self, role: str) -> bool:
        return role == "admin" or role in VESSEL_MANAGE_ROLES

    def validate_vessel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "name")
        number(p, "spare_cable_km", 0)
        return p

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

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        resource_op: Optional[Dict[str, Any]] = None
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            vessel_name = text(data, "vessel_name")
            planned_start = parse_time_value(text(data, "planned_start"))
            planned_end = parse_time_value(text(data, "planned_end"))
            if planned_end <= planned_start:
                raise ValidationError("计划结束时间必须大于开始时间")
            changes["vessel_name"] = vessel_name
            changes["planned_start"] = planned_start.isoformat()
            changes["planned_end"] = planned_end.isoformat()
            changes["reserved_spare_km"] = float(p["required_spare_km"])
            resource_op = {"kind": "reserve", "vessel_name": vessel_name, "planned_start": changes["planned_start"], "planned_end": changes["planned_end"], "required_km": float(p["required_spare_km"])}
            summary = "抢修方案已批准" if record["state"] == "detected" else "已改派抢修船与计划时段，原安排资源已释放"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            onboard = number(data, "available_spare_km", 0)
            if onboard < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            vessel_name = optional_text(data, "vessel_name") or p.get("vessel_name", "")
            if p.get("vessel_name") and vessel_name != p["vessel_name"]:
                raise ValidationError("动员船舶必须与审批选定船舶一致")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = vessel_name
            changes["onboard_spare_km"] = onboard
            resource_op = {"kind": "adjust", "actual_km": onboard}
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
            used = number(data, "spare_used_km", 0)
            if used < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            onboard = float(p.get("onboard_spare_km", p.get("reserved_spare_km", 0)))
            if used > onboard:
                raise ValidationError("备缆消耗超过船上实际备缆")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = used
            resource_op = {"kind": "consume", "used_km": used}
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
            resource_op = {"kind": "release"}
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            resource_op = {"kind": "release"}
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), resource_op
