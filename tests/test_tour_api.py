import json
import tempfile
import threading
import unittest
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

from exhibition_tour.api import Handler, route
from exhibition_tour.clock import FixedClock
from exhibition_tour.service import TourScheduler
from exhibition_tour.storage import Database
from http.server import ThreadingHTTPServer

from datetime import datetime, timezone


def _seed(service: TourScheduler) -> None:
    service.register_participant(request_id="p0", actor_id="bootstrap", participant_id="op",
                                 display_name="运营", role="operator", organization_id="org-op")
    for pid, name, role, org in [
            ("vsh", "上海场馆", "venue", "org-sh"), ("ins", "保险", "insurer", "org-ins"),
            ("rep", "代表", "representative", "org-rep")]:
        service.register_participant(request_id=f"p-{pid}", actor_id="op", participant_id=pid,
                                     display_name=name, role=role, organization_id=org)
    service.register_artwork(request_id="a1", actor_id="op", artwork_id="art", title="作品",
                             representative_id="rep", components=["主机"])
    service.register_resource(request_id="r1", actor_id="op", resource_id="wsh", kind="venue_window",
                              owner_id="vsh", label="沪窗",
                              capabilities={"city": "上海",
                                            "window_start": "2026-11-01T00:00:00Z",
                                            "window_end": "2026-12-31T00:00:00Z"})
    service.register_resource(request_id="r2", actor_id="op", resource_id="csh", kind="display_case",
                              owner_id="vsh", label="沪柜", capabilities={"city": "上海"})
    service.register_resource(request_id="r3", actor_id="op", resource_id="q1",
                              kind="insurance_quota", owner_id="ins", label="额度",
                              capabilities={"capacity": 1000000, "currency": "CNY"})
    service.upsert_qualification(request_id="q1q", actor_id="op", venue_organization_id="org-sh",
                                 artwork_id="art", qualified=True)
    service.create_plan_version(
        request_id="pl1", actor_id="op", plan_id="tour", artwork_id="art", components=["主机"],
        segments=[
            {"segment_id": "s1", "kind": "venue",
             "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-12T20:00:00Z",
             "resource_id": "wsh",
             "requirement": {"case_resource_id": "csh", "install_hours": 4, "dismantle_hours": 4}},
            {"segment_id": "s2", "kind": "insurance",
             "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-12T20:00:00Z",
             "resource_id": "q1",
             "requirement": {"coverage_amount": 100, "currency": "CNY"}},
        ],
        sessions={"s1": [{"starts_at": "2026-11-11T10:00:00Z", "label": "公场",
                          "capacity": 50}]},
        lease_minutes=600)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = TourScheduler(self.database,
                                     FixedClock(datetime(2026, 11, 1, tzinfo=timezone.utc)))
        _seed(self.service)

    def tearDown(self):
        self.database.close()

    def test_health_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_todos_are_scoped_to_actor_and_minimal(self):
        status, payload = route(self.service, "GET", "/todos", None, {"X-Actor-Id": "vsh"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("s1", payload["items"][0]["segment_id"])
        self.assertNotIn("requirement", payload["items"][0]["minimal"])

        status, payload = route(self.service, "GET", "/todos", None, {"X-Actor-Id": "ins"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual(100, payload["items"][0]["minimal"]["coverage_amount"])

    def test_confirm_then_publish_flow(self):
        for actor, seg in (("vsh", "s1"), ("ins", "s2")):
            status, payload = route(
                self.service, "POST", f"/plans/tour/respond",
                {"request_id": f"c-{actor}", "version": 1, "segment_id": seg,
                 "decision": "confirm"}, {"X-Actor-Id": actor})
            self.assertEqual(200, status, payload)
        status, payload = route(
            self.service, "POST", "/plans/tour/respond",
            {"request_id": "c-rep", "version": 1, "segment_id": "s1",
             "decision": "confirm"}, {"X-Actor-Id": "rep"})
        self.assertEqual(200, status)
        status, payload = route(
            self.service, "POST", "/plans/tour/respond",
            {"request_id": "c-rep2", "version": 1, "segment_id": "s2",
             "decision": "confirm"}, {"X-Actor-Id": "rep"})
        self.assertEqual(200, status)
        status, payload = route(self.service, "POST", "/plans/tour/publish",
                                {"request_id": "pub", "version": 1}, {"X-Actor-Id": "op"})
        self.assertEqual(200, status, payload)

    def test_conflict_includes_blockers(self):
        status, payload = route(
            self.service, "POST", "/plans",
            {"request_id": "dup", "plan_id": "tour2", "artwork_id": "art",
             "components": ["主机"],
             "segments": [
                 {"segment_id": "d1", "kind": "venue",
                  "start_at": "2026-11-10T09:00:00Z", "end_at": "2026-11-12T12:00:00Z",
                  "resource_id": "wsh",
                  "requirement": {"case_resource_id": "csh", "install_hours": 2,
                                  "dismantle_hours": 2}},
                 {"segment_id": "d2", "kind": "insurance",
                  "start_at": "2026-11-10T09:00:00Z", "end_at": "2026-11-12T12:00:00Z",
                  "resource_id": "q1",
                  "requirement": {"coverage_amount": 100, "currency": "CNY"}},
             ],
             "sessions": {"d1": [{"starts_at": "2026-11-11T10:00:00Z", "label": "撞",
                                  "capacity": 1}]}},
            {"X-Actor-Id": "op"})
        self.assertEqual(409, status)
        self.assertTrue(any(b["resource_id"] == "wsh" for b in payload["blockers"]))

    def test_unknown_route_and_missing_param(self):
        status, payload = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        status, payload = route(self.service, "GET", "/plans/tour/snapshot", None,
                                {"X-Actor-Id": "op"})
        self.assertEqual(400, status)


class HttpServerTest(unittest.TestCase):
    def test_http_roundtrip_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "http.sqlite3")
            database = Database(db_path)
            service = TourScheduler(database, FixedClock(datetime(2026, 11, 1, tzinfo=timezone.utc)))
            _seed(service)
            Handler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def call(method: str, path: str, body: dict | None = None, actor: str = "op"):
                    data = json.dumps(body or {}).encode("utf-8")
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}", data=data if method == "POST" else None,
                        method=method,
                        headers={"Content-Type": "application/json", "X-Actor-Id": actor})
                    with urllib.request.urlopen(request) as response:
                        return response.status, json.loads(response.read())

                status, _ = call("GET", "/health")
                self.assertEqual(200, status)
                # 重复 request_id 必须返回同一条回执。
                body = {"request_id": "rx", "participant_id": "px", "display_name": "新场馆",
                        "role": "venue", "organization_id": "org-x"}
                first = call("POST", "/participants", body)[1]
                second = call("POST", "/participants", body)[1]
                self.assertFalse(first["replayed"])
                self.assertTrue(second["replayed"])
                self.assertEqual(first["resource_id"], second["resource_id"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                database.close()

            # 重启：租约与待办仍然可见。
            database = Database(db_path)
            service = TourScheduler(database, FixedClock(datetime(2026, 11, 1, tzinfo=timezone.utc)))
            todos = service.list_todos(actor_id="vsh")
            self.assertEqual(1, len(todos))
            valid, _ = service.verify_audit()
            self.assertTrue(valid)
            database.close()


if __name__ == "__main__":
    unittest.main()
