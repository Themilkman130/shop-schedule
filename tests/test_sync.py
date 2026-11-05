"""Tests for the ShopMonkey -> board mapping. All data here is made up."""
import unittest
from datetime import datetime, timezone

from shop_sync.sync import desired_state, labor_summary, pencil_matches, plan_writes
from zoneinfo import ZoneInfo

CFG = {
    "timezone": "America/Los_Angeles",
    "in_shop_statuses": ["Diagnosis", "Parts Hold", "In Progress", "Ready For Pickup"],
    "upcoming_statuses": ["New Work Request", "Scheduled"],
    "techs": ["Ann B.", "Cal D."],
    "default_tech_hours": 8,
    "appointment_days_ahead": 90,
    "request_gap_seconds": 0,
    "max_writes_per_batch": 3,
}
NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)  # 10:00 Pacific
TZ = ZoneInfo("America/Los_Angeles")

STATUSES = [{"id": f"w{i}", "name": n} for i, n in enumerate(
    ["New Work Request", "Scheduled", "Diagnosis", "Parts Hold", "In Progress", "Ready For Pickup", "AR DUE"])]
USERS = [
    {"id": "u1", "firstName": "Ann", "lastName": "Baker"},
    {"id": "u2", "firstName": "Cal", "lastName": " Diaz"},
    {"id": "u3", "firstName": "Sam", "lastName": "Writer"},
]


def svc(status, *lines, **kw):
    return {"authorizationStatus": status, "labors": [
        {"hours": h, "technicianId": t, "completed": done} for h, t, done in lines], **kw}


def order(oid, wf, **kw):
    return {"id": oid, "number": oid[-3:], "workflowStatusId": wf, "archived": False, "deleted": False,
            "vehicleId": "v-" + oid, "customerId": "c-" + oid, "assignedTechnicianIds": [],
            "workflowStatusDate": "2026-11-05T15:00:00Z", "dueDate": None, "name": "Brakes", **kw}


def snapshot(**over):
    sm = {
        "statuses": STATUSES, "users": USERS,
        "orders": [
            order("o-101", "w4", assignedTechnicianIds=["u1", "u2"]),
            order("o-102", "w6"),  # AR DUE: off the lot
            order("o-103", "w1"),  # Scheduled: upcoming only
        ],
        "services": {
            "o-101": [
                svc("Authorized", (2, "u1", False), (1, "u1", True), (1.5, "u2", False)),
                svc("NotAuthorized", (3, "u2", False), (0.5, "u3", False)),
                svc("NotAuthorized", (9, "u1", False), deferredDate="2026-11-01"),
            ],
            "o-103": [svc("NotAuthorized", (4, "u1", False))],
        },
        "vehicles": [{"id": "v-o-101", "year": 2019, "make": "Ford", "model": "F-150", "color": "White"}],
        "customers": [{"id": "c-o-101", "lastName": "Smith", "firstName": "Jo"}],
        "appointments": [
            {"id": "a1", "orderId": "o-103", "order": order("o-103", "w1"), "startDate": "2026-11-06T16:00:00Z",
             "endDate": "2026-11-06T17:00:00Z", "customer": {"lastName": "Jones"},
             "vehicle": {"year": 2020, "make": "Honda", "model": "CR-V"}, "name": "60k service"},
            {"id": "a2", "orderId": "o-101", "order": order("o-101", "w4"), "startDate": "2026-11-05T16:00:00Z",
             "customer": {"lastName": "Smith"}, "vehicle": {}},
            {"id": "a3", "orderId": None, "order": None, "startDate": "2026-11-07T17:00:00Z",
             "customer": {"companyName": "Acme Fleet"}, "vehicle": {}, "name": "Fleet check"},
            {"id": "a4", "orderId": None, "order": None, "startDate": "2026-11-08T17:00:00Z",
             "customer": {"lastName": "Gone"}, "vehicle": {}, "cancellationNote": "called off"},
        ],
    }
    sm.update(over)
    return sm


class LaborTests(unittest.TestCase):
    def test_split_per_tech_and_authorization(self):
        lab = labor_summary(snapshot()["services"]["o-101"], {"u1": "Ann B.", "u2": "Cal D."})
        self.assertEqual(lab["smHoursLeft"], 7.0)  # 2 + 1.5 + 3 + 0.5; done and deferred lines skipped
        self.assertEqual(lab["smAuthHoursLeft"], 3.5)
        self.assertEqual(lab["techHours"], {"Ann B.": {"left": 2.0, "auth": 2.0}, "Cal D.": {"left": 4.5, "auth": 1.5}})
        self.assertEqual(lab["unassignedHours"], 0.5)


class DesiredTests(unittest.TestCase):
    def setUp(self):
        self.d = desired_state(snapshot(), CFG)

    def test_only_in_shop_orders_become_vehicles(self):
        self.assertEqual(list(self.d["vehicles"]), ["sm-o-o-101"])
        v = self.d["vehicles"]["sm-o-o-101"]
        self.assertEqual((v["stage"], v["customer"], v["make"], v["year"]), ("In Progress", "Smith", "Ford", "2019"))
        self.assertEqual(v["tech"], "Cal D.")  # most hours left
        self.assertEqual(v["orderTechs"], ["Ann B.", "Cal D."])

    def test_appointments_skip_arrived_and_cancelled(self):
        self.assertEqual(sorted(self.d["appointments"]), ["sm-a-a1", "sm-a-a3"])
        a1 = self.d["appointments"]["sm-a-a1"]
        self.assertEqual((a1["hours"], a1["authHours"], a1["tech"], a1["day"]), (4.0, 0.0, "Ann B.", "2026-11-06"))
        self.assertEqual(self.d["appointments"]["sm-a-a3"]["customer"], "Acme Fleet")

    def test_tech_names_tolerate_spacing(self):
        self.assertEqual(self.d["techLabels"], ["Ann B.", "Cal D."])


class PencilTests(unittest.TestCase):
    def test_same_customer_same_local_day(self):
        p = {"customer": "smith", "start": "2026-11-06T00:30:00Z"}  # Nov 5, 4:30 pm Pacific
        self.assertTrue(pencil_matches(p, {"customer": "Smith", "start": "2026-11-05T16:00:00Z"}, TZ))
        self.assertFalse(pencil_matches(p, {"customer": "Smith", "start": "2026-11-06T16:00:00Z"}, TZ))
        self.assertFalse(pencil_matches(p, {"customer": "Jones", "start": "2026-11-05T16:00:00Z"}, TZ))


class PlanTests(unittest.TestCase):
    def board(self):
        return {
            "vehicles": {
                "s1": {"sample": True, "ro": "1"},
                "sm-o-o-101": {"source": "shopmonkey", "stage": "Diagnosis", "spot": "Bay 2", "note": "keep me",
                               "hoursLeft": 3},
                "sm-o-old": {"source": "shopmonkey", "stage": "Ready For Pickup"},
                "hand": {"ro": "999"},
            },
            "appointments": {
                "p1": {"tentative": True, "customer": "Jones", "start": "2026-11-06T18:00:00Z"},
                "p2": {"tentative": True, "customer": "Nobody", "start": "2026-11-06T18:00:00Z"},
                "sa1": {"sample": True},
            },
            "config": {"shop": {"sample": True, "open": "07:30", "techs": [{"name": "Mike"}]}},
        }

    def versions(self, board):
        return {f"{c}/{i}": 4 for c, docs in board.items() for i in docs}

    def plan(self):
        b = self.board()
        return plan_writes(desired_state(snapshot(), CFG), b, self.versions(b), CFG, NOW)

    def writes(self, r):
        return [w for b in r["batches"] for w in b]

    def test_board_fields_never_written(self):
        upd = next(w for w in self.writes(self.plan()) if w["doc_id"] == "sm-o-o-101")
        self.assertEqual(upd["op"], "update")
        self.assertEqual(upd["if_version"], 4)
        for k in ("spot", "note", "schedOrder", "hoursLeft"):
            self.assertNotIn(k, upd["data"])
        self.assertEqual(upd["data"]["stage"], "In Progress")

    def test_removals(self):
        ops = {(w["collection"], w["doc_id"]): w["op"] for w in self.writes(self.plan())}
        self.assertEqual(ops[("vehicles", "sm-o-old")], "delete")
        self.assertEqual(ops[("vehicles", "s1")], "delete")
        self.assertEqual(ops[("appointments", "sa1")], "delete")
        self.assertEqual(ops[("appointments", "p1")], "delete")  # matched appointment a1
        self.assertNotIn(("appointments", "p2"), ops)
        self.assertNotIn(("vehicles", "hand"), ops)  # not ours: left alone

    def test_new_docs_have_no_version_and_batches_are_capped(self):
        r = self.plan()
        self.assertTrue(all(len(b) <= 3 for b in r["batches"]))
        new = [w for w in self.writes(r) if w["doc_id"].startswith("sm-a-")]
        self.assertTrue(new and all(w["op"] == "set" and "if_version" not in w for w in new))

    def test_sample_techs_replaced_keeping_shop_hours(self):
        cfgw = next(w for w in self.writes(self.plan()) if w["collection"] == "config")
        self.assertEqual([t["name"] for t in cfgw["data"]["techs"]], ["Ann B.", "Cal D."])
        self.assertEqual(cfgw["data"]["open"], "07:30")
        self.assertNotIn("sample", cfgw["data"])

    def test_second_run_is_quiet(self):
        b = self.board()
        d = desired_state(snapshot(), CFG)
        first = plan_writes(d, b, self.versions(b), CFG, NOW)
        for w in self.writes(first):
            docs = b.setdefault(w["collection"], {})
            if w["op"] == "delete":
                docs.pop(w["doc_id"])
            elif w["op"] == "set":
                docs[w["doc_id"]] = dict(w["data"])
            else:
                docs[w["doc_id"]].update(w["data"])
        second = plan_writes(d, b, self.versions(b), CFG, NOW)
        self.assertEqual(self.writes(second), [])

    def test_missing_version_is_an_error(self):
        b = self.board()
        with self.assertRaises(ValueError):
            plan_writes(desired_state(snapshot(), CFG), b, {}, CFG, NOW)

    def test_empty_shopmonkey_keeps_samples(self):
        b = self.board()
        empty = desired_state(snapshot(orders=[], appointments=[]), CFG)
        ops = {(w["collection"], w["doc_id"]) for w in self.writes(plan_writes(empty, b, self.versions(b), CFG, NOW))}
        self.assertNotIn(("vehicles", "s1"), ops)
        self.assertNotIn(("config", "shop"), ops)


if __name__ == "__main__":
    unittest.main()
