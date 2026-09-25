import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
APPROVE_DATA = {'repair_manager': 'RM-2', 'vessel_name': 'CS-1', 'planned_start': '2026-09-26T08:00:00', 'planned_end': '2026-09-28T08:00:00'}
MANAGER = Actor("planner", "repair_manager")
OPERATOR = Actor("creator", "noc_operator")
MASTER = Actor("master", "vessel_master")
ENGINEER = Actor("engineer", "cable_engineer")


class ResourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.service.create_vessel(MANAGER, {"name": "CS-1", "spare_cable_km": 40})
        self.service.create_vessel(MANAGER, {"name": "CS-2", "spare_cable_km": 20})

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, **overrides):
        data = dict(CREATE_DATA)
        data.update(overrides)
        return self.service.create(OPERATOR, reference, data)

    def _approve(self, record, **overrides):
        data = dict(APPROVE_DATA)
        data.update(overrides)
        return self.service.act(MANAGER, record["id"], record["version"], "approve", data)

    def test_double_booking_names_blocking_record(self):
        first = self._approve(self._create("CABLE-1"))
        second = self._create("CABLE-2", segment="S4", start_km=200.0, end_km=210.0)
        with self.assertRaises(Conflict) as ctx:
            self._approve(second)
        self.assertIn("#%s" % first["id"], str(ctx.exception))
        stayed = self.service.get_record(OPERATOR, second["id"])
        self.assertEqual(stayed["state"], "detected")

    def test_insufficient_spare_blocks_approval(self):
        self.service.create_vessel(MANAGER, {"name": "CS-3", "spare_cable_km": 10})
        record = self._create("CABLE-1")
        with self.assertRaises(Conflict) as ctx:
            self._approve(record, vessel_name="CS-3")
        self.assertIn("不足", str(ctx.exception))

    def test_non_overlapping_windows_share_vessel(self):
        self._approve(self._create("CABLE-1"))
        second = self._create("CABLE-2", segment="S4", start_km=200.0, end_km=210.0)
        second = self._approve(second, planned_start="2026-09-29T08:00:00", planned_end="2026-09-30T08:00:00")
        self.assertEqual(second["state"], "approved")
        self.assertEqual(self.service.get_vessel(OPERATOR, "CS-1")["spare_cable_km"], 13.75)

    def test_reassign_releases_previous_reservation(self):
        record = self._approve(self._create("CABLE-1"))
        self.assertEqual(self.service.get_vessel(OPERATOR, "CS-1")["spare_cable_km"], 24.25)
        updated = self._approve(record, vessel_name="CS-2", planned_start="2026-10-01T08:00:00", planned_end="2026-10-03T08:00:00")
        self.assertEqual(updated["state"], "approved")
        self.assertEqual(updated["payload"]["vessel_name"], "CS-2")
        cs1 = self.service.get_vessel(OPERATOR, "CS-1")
        self.assertEqual(cs1["spare_cable_km"], 40)
        self.assertTrue(all(item["status"] == "released" for item in cs1["reservations"]))
        self.assertEqual(self.service.get_vessel(OPERATOR, "CS-2")["spare_cable_km"], 4.25)

    def test_consumption_and_return_on_restore(self):
        record = self._approve(self._create("CABLE-1"))
        record = self.service.act(MASTER, record["id"], record["version"], "mobilize", {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"})
        self.assertEqual(self.service.get_vessel(OPERATOR, "CS-1")["spare_cable_km"], 22.0)
        record = self.service.act(ENGINEER, record["id"], record["version"], "survey", {"survey_complete": True, "fault_location_km": 128})
        record = self.service.act(ENGINEER, record["id"], record["version"], "splice", {"splice_loss_db": 0.12, "spare_used_km": 16})
        record = self.service.act(OPERATOR, record["id"], record["version"], "test", {"end_to_end_loss_db": 0.3})
        record = self.service.act(OPERATOR, record["id"], record["version"], "restore", {"traffic_restored": True, "restore_capacity_gbps": 400})
        vessel = self.service.get_vessel(OPERATOR, "CS-1")
        self.assertEqual(vessel["spare_cable_km"], 24.0)
        reservation = vessel["reservations"][0]
        self.assertEqual(reservation["status"], "released")
        self.assertEqual(reservation["consumed_km"], 16.0)
        self.assertEqual(record["payload"]["vessel_name"], "CS-1")
        self.assertEqual(record["payload"]["planned_start"], "2026-09-26T08:00:00+00:00")
        self.assertEqual(record["payload"]["spare_used_km"], 16.0)
        self.assertEqual(record["payload"]["restore_capacity_gbps"], 400)
        timeline = self.service.timeline(OPERATOR, record["id"])
        approve_event = next(item for item in timeline if item["action"] == "approve")
        self.assertEqual(approve_event["details"]["resource"]["reserved_km"], 15.75)
        restore_event = next(item for item in timeline if item["action"] == "restore")
        self.assertEqual(restore_event["details"]["resource"]["returned_km"], 2.0)

    def test_cancel_returns_reserved_spare(self):
        record = self._approve(self._create("CABLE-1"))
        record = self.service.act(MANAGER, record["id"], record["version"], "cancel", {"cancel_reason": "海况恶化"})
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(self.service.get_vessel(OPERATOR, "CS-1")["spare_cable_km"], 40)

    def test_mobilize_must_use_approved_vessel(self):
        record = self._approve(self._create("CABLE-1"))
        with self.assertRaises(ValidationError):
            self.service.act(MASTER, record["id"], record["version"], "mobilize", {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-2"})

    def test_vessel_list_shows_active_reservations(self):
        record = self._approve(self._create("CABLE-1"))
        vessels = {item["name"]: item for item in self.service.list_vessels(OPERATOR)}
        active = vessels["CS-1"]["active_reservations"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["record_id"], record["id"])
        self.assertEqual(active[0]["planned_start"], "2026-09-26T08:00:00+00:00")
        self.assertEqual(active[0]["reserved_km"], 15.75)
