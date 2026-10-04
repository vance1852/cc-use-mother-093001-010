"""巡展调度 HTTP 路由测试。"""

from __future__ import annotations

import threading
import unittest

from creative_program_foundation.api import route

from tests.test_tour import T0, TourFixture


def iso_z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def call(route_fn, method, path, body=None, actor="op1"):
    return route_fn(method, path, body or {}, {"X-Actor-Id": actor})


class TourApiTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TourFixture()
        self.route_fn = lambda *a, **k: route(
            self.ctx.svc, a[0], a[1], a[2] if len(a) > 2 else {},
            a[3] if len(a) > 3 else k.get("headers", {"X-Actor-Id": "op1"}),
            tour=self.ctx.tour)

    def tearDown(self):
        self.ctx.close()

    def test_register_and_draft_over_http(self):
        status, body = self.route_fn(
            "POST", "/tour/plans/drafts",
            {"request_id": "http-draft", "manifest": self.ctx.manifest()})
        self.assertEqual(201, status)
        self.assertEqual("draft", body["status"])

    def test_todos_endpoint_scoped_to_actor_header(self):
        self.route_fn("POST", "/tour/plans/drafts",
                      {"request_id": "http-draft", "manifest": self.ctx.manifest()})
        status, body = self.route_fn("GET", "/tour/todos", {}, {"X-Actor-Id": "rep1"})
        self.assertEqual(200, status)
        self.assertEqual("representative", body["items"][0]["party_kind"])
        status, body = self.route_fn("GET", "/tour/todos", {}, {"X-Actor-Id": "ins1"})
        self.assertEqual([], body["items"])

    def test_conflicts_endpoint_returns_recovery(self):
        self.route_fn("POST", "/tour/plans/drafts",
                      {"request_id": "d1", "manifest": self.ctx.manifest()})
        self.route_fn("POST", "/tour/plans/drafts",
                      {"request_id": "d2", "manifest": self.ctx.manifest("tour-002")})
        status, body = self.route_fn("GET", "/tour/conflicts?plan_id=tour-002",
                                     {}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertTrue(body["items"][0]["detail"]["recovery"])

    def test_restore_endpoint(self):
        self.route_fn("POST", "/tour/plans/drafts",
                      {"request_id": "d1", "manifest": self.ctx.manifest()})
        status, body = self.route_fn(
            "GET", f"/tour/restore?plan_id=tour-001&at={iso_z(T0)}",
            {}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertIn("responsibility_chain", body)


class ConcurrentPublishTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TourFixture()

    def tearDown(self):
        self.ctx.close()

    def test_concurrent_drafts_for_same_resources_serialize(self):
        ctx = self.ctx
        results: list = []
        errors: list = []
        barrier = threading.Barrier(2)

        def worker(plan_id, request_id):
            try:
                barrier.wait(timeout=5)
                results.append(ctx.tour.create_draft(
                    request_id=request_id, actor_id="op1", manifest=ctx.manifest(plan_id)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("tour-a", "da")),
                   threading.Thread(target=worker, args=("tour-b", "db"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        statuses = sorted(r["status"] for r in results)
        # 一个抢到资源成为 draft，另一个必然 blocked
        self.assertEqual(["blocked", "draft"], statuses)
        effective_resource = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM resource_allocations WHERE status='held' "
            "AND resource_type='venue_window' AND resource_id='win-a10'").fetchone()["c"]
        self.assertEqual(1, effective_resource)

    def test_concurrent_publish_same_version_single_effective(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        ctx.confirm_all()
        outcomes: list = []
        barrier = threading.Barrier(2)

        def worker(request_id):
            try:
                barrier.wait(timeout=5)
                outcomes.append(ctx.tour.publish(
                    request_id=request_id, actor_id="op1",
                    plan_id="tour-001", version=1))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=worker, args=("pa",)),
                   threading.Thread(target=worker, args=("pb",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        real = [o for o in outcomes if not isinstance(o, Exception) and not o.get("duplicate")]
        duplicates = [o for o in outcomes if not isinstance(o, Exception) and o.get("duplicate")]
        self.assertEqual(1, len(real), outcomes)
        self.assertEqual(1, len(duplicates), outcomes)
        effective = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM plan_effective WHERE plan_id='tour-001'").fetchone()["c"]
        self.assertEqual(1, effective)
        # 资源只被确认一次，没有重复占用
        confirmed = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM resource_allocations WHERE plan_id='tour-001' "
            "AND version=1 AND status='confirmed'").fetchone()["c"]
        held = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM resource_allocations WHERE plan_id='tour-001' "
            "AND version=1 AND status='held'").fetchone()["c"]
        self.assertEqual(0, held)
        self.assertGreater(confirmed, 0)

    def test_two_drafts_only_newer_can_become_effective(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        ctx.confirm_all()
        ctx.publish_plan()
        # 用手动重排生成版本 2
        ctx.advance(86400)
        ctx.tour.replan(request_id="rp", actor_id="op1", plan_id="tour-001",
                        stop_overrides={"stop2": 1})
        # 版本 2 仅完成部分确认，同时再次尝试发布旧版本 1（已 superseded 不适用，
        # 这里断言：未确认完的版本 2 不能发布）
        with self.assertRaises(Exception):
            ctx.tour.publish(request_id="pub2-blocked", actor_id="op1",
                             plan_id="tour-001", version=2)
        # 完成 v2 确认后生效，生效版本唯一
        ctx.tour.confirm(request_id="c2r", actor_id="rep1", plan_id="tour-001", version=2)
        ctx.tour.confirm(request_id="c2v", actor_id="venue2", plan_id="tour-001", version=2)
        ctx.tour.publish(request_id="pub2", actor_id="op1", plan_id="tour-001", version=2)
        row = ctx.database.connection.execute(
            "SELECT version FROM plan_effective WHERE plan_id='tour-001'").fetchone()
        self.assertEqual(2, row["version"])
        published = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM tour_plans WHERE plan_id='tour-001' AND status='published'"
        ).fetchone()["c"]
        self.assertEqual(1, published)


if __name__ == "__main__":
    unittest.main()
