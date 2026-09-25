import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ResourceConflict, ValidationError


CREATE_A = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
CREATE_B = {'cable': 'SEA-2', 'segment': 'S1', 'start_km': 40.0, 'end_km': 55.0, 'depth_m': 1500.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
W1 = ('2026-02-01T00:00:00Z', '2026-02-03T00:00:00Z')
W1_OVERLAP = ('2026-02-02T00:00:00Z', '2026-02-04T00:00:00Z')
W2 = ('2026-02-10T00:00:00Z', '2026-02-12T00:00:00Z')
MANAGER = Actor("mgr", "repair_manager")
OPERATOR = Actor("noc", "noc_operator")


class ResourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.service.register_vessel(Actor("admin", "admin"), {"name": "CS-1", "spare_capacity_km": 30})
        self.service.register_vessel(Actor("admin", "admin"), {"name": "CS-2", "spare_capacity_km": 30})

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, data):
        return self.service.create(OPERATOR, reference, data)

    def _approve(self, record, vessel, window):
        data = {'repair_manager': 'RM-2', 'vessel_name': vessel, 'planned_start': window[0], 'planned_end': window[1]}
        return self.service.act(MANAGER, record["id"], record["version"], "approve", data)

    def _vessel(self, name):
        return self.service.get_vessel(MANAGER, name)

    def test_schedule_conflict_stops_record_and_names_blocker(self):
        record_a = self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        record_b = self._create("CABLE-40002", CREATE_B)
        with self.assertRaises(ResourceConflict) as ctx:
            self._approve(record_b, "CS-1", W1_OVERLAP)
        message = str(ctx.exception)
        self.assertIn("CS-1", message)
        self.assertIn("CABLE-40001", message)
        record_b = self.service.get_record(OPERATOR, record_b["id"])
        self.assertEqual(record_b["state"], "detected")
        timeline = self.service.timeline(OPERATOR, record_b["id"])
        self.assertEqual(timeline[-1]["action"], "resource_blocked")
        self.assertEqual(timeline[-1]["details"]["blockers"][0]["reference"], "CABLE-40001")
        self.assertEqual(record_a["state"], "approved")

    def test_spare_shortage_names_holder(self):
        self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        record_b = self._create("CABLE-40002", CREATE_B)
        with self.assertRaises(ResourceConflict) as ctx:
            self._approve(record_b, "CS-1", W2)
        message = str(ctx.exception)
        self.assertIn("剩余备缆14.25km", message)
        self.assertIn("CABLE-40001", message)
        self.assertEqual(self.service.get_record(OPERATOR, record_b["id"])["state"], "detected")

    def test_reassign_releases_original_and_frees_other_approval(self):
        record_a = self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        record_a = self.service.act(MANAGER, record_a["id"], record_a["version"], "reassign",
                                    {'vessel_name': 'CS-2', 'planned_start': W1[0], 'planned_end': W1[1]})
        self.assertEqual(record_a["state"], "approved")
        self.assertEqual(record_a["payload"]["vessel_name"], "CS-2")
        self.assertEqual(self._vessel("CS-1")["spare_remaining_km"], 30.0)
        self.assertEqual(self._vessel("CS-2")["spare_remaining_km"], 14.25)
        record_b = self._approve(self._create("CABLE-40002", CREATE_B), "CS-1", W1_OVERLAP)
        self.assertEqual(record_b["state"], "approved")

    def test_reassign_after_mobilize_returns_to_approved(self):
        record = self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        record = self.service.act(Actor("master", "vessel_master"), record["id"], record["version"], "mobilize",
                                  {'weather_window_hours': 40, 'available_spare_km': 18})
        self.assertEqual(self._vessel("CS-1")["spare_reserved_km"], 18.0)
        record = self.service.act(MANAGER, record["id"], record["version"], "reassign",
                                  {'vessel_name': 'CS-2', 'planned_start': W1[0], 'planned_end': W1[1]})
        self.assertEqual(record["state"], "approved")
        self.assertNotIn("weather_window_hours", record["payload"])
        self.assertEqual(self._vessel("CS-1")["spare_remaining_km"], 30.0)
        self.assertEqual(self._vessel("CS-2")["spare_reserved_km"], 15.75)

    def test_actual_usage_deducted_and_unused_returned(self):
        record = self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        self.assertEqual(self._vessel("CS-1")["spare_remaining_km"], 14.25)
        record = self.service.act(Actor("master", "vessel_master"), record["id"], record["version"], "mobilize",
                                  {'weather_window_hours': 40, 'available_spare_km': 18})
        self.assertEqual(self._vessel("CS-1")["spare_remaining_km"], 12.0)
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "survey",
                                  {'survey_complete': True, 'fault_location_km': 128})
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "splice",
                                  {'splice_loss_db': 0.12, 'spare_used_km': 16})
        vessel = self._vessel("CS-1")
        self.assertEqual(vessel["spare_consumed_km"], 16.0)
        self.assertEqual(vessel["spare_reserved_km"], 2.0)
        record = self.service.act(OPERATOR, record["id"], record["version"], "test", {'end_to_end_loss_db': 0.3})
        record = self.service.act(OPERATOR, record["id"], record["version"], "restore",
                                  {'traffic_restored': True, 'restore_capacity_gbps': 400})
        vessel = self._vessel("CS-1")
        self.assertEqual(vessel["spare_reserved_km"], 0.0)
        self.assertEqual(vessel["spare_remaining_km"], 14.0)
        detail = self.service.get_record(OPERATOR, record["id"])
        self.assertEqual(detail["resource"]["vessel_name"], "CS-1")
        self.assertEqual(detail["resource"]["planned_start"], "2026-02-01T00:00:00+00:00")
        self.assertEqual(detail["resource"]["spare_consumed_km"], 16.0)
        self.assertEqual(detail["resource"]["vessel_remaining_km"], 14.0)
        self.assertEqual(detail["resource"]["restore_capacity_gbps"], 400)
        listed = self.service.list_records(OPERATOR)
        self.assertEqual(listed[0]["resource"]["spare_consumed_km"], 16.0)
        self.assertEqual(listed[0]["resource"]["vessel_remaining_km"], 14.0)

    def test_cancel_returns_reserved_spare(self):
        record = self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        record = self.service.act(MANAGER, record["id"], record["version"], "cancel", {'cancel_reason': '海况恶化'})
        vessel = self._vessel("CS-1")
        self.assertEqual(vessel["spare_reserved_km"], 0.0)
        self.assertEqual(vessel["spare_remaining_km"], 30.0)
        self.assertEqual(record["payload"]["spare_reserved_km"], 0.0)

    def test_unknown_vessel_rejected(self):
        record = self._create("CABLE-40001", CREATE_A)
        with self.assertRaises(ValidationError):
            self._approve(record, "CS-9", W1)
        self.assertEqual(self.service.get_record(OPERATOR, record["id"])["state"], "detected")

    def test_splice_cannot_exceed_onboard_spare(self):
        record = self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        record = self.service.act(Actor("master", "vessel_master"), record["id"], record["version"], "mobilize",
                                  {'weather_window_hours': 40, 'available_spare_km': 18})
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "survey",
                                  {'survey_complete': True, 'fault_location_km': 128})
        with self.assertRaises(ValidationError):
            self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "splice",
                             {'splice_loss_db': 0.12, 'spare_used_km': 19})

    def test_vessel_allocations_visible_in_detail(self):
        self._approve(self._create("CABLE-40001", CREATE_A), "CS-1", W1)
        vessel = self._vessel("CS-1")
        self.assertEqual(len(vessel["allocations"]), 1)
        allocation = vessel["allocations"][0]
        self.assertEqual(allocation["reference"], "CABLE-40001")
        self.assertEqual(allocation["planned_start"], "2026-02-01T00:00:00+00:00")
        self.assertEqual(allocation["spare_reserved_km"], 15.75)
        names = [item["name"] for item in self.service.list_vessels(OPERATOR)]
        self.assertEqual(names, ["CS-1", "CS-2"])

    def test_vessel_registration_rules(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_vessel(OPERATOR, {"name": "CS-3", "spare_capacity_km": 10})
        with self.assertRaises(Conflict):
            self.service.register_vessel(MANAGER, {"name": "CS-1", "spare_capacity_km": 10})
