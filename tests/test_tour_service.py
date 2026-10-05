import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from exhibition_tour.clock import FixedClock
from exhibition_tour.errors import (
    ConflictError, PermissionDenied, StateError, ValidationError)
from exhibition_tour.service import TourScheduler
from exhibition_tour.storage import Database


def _bootstrap(service: TourScheduler) -> None:
    service.register_participant(request_id="p0", actor_id="bootstrap", participant_id="op",
                                 display_name="运营", role="operator", organization_id="org-op")
    people = [
        ("vsh", "上海场馆", "venue", "org-sh"),
        ("vbj", "北京博物馆", "venue", "org-bj"),
        ("vbj2", "北京茶文化空间", "venue", "org-bj2"),
        ("car", "承运", "carrier", "org-car"),
        ("ins", "保险", "insurer", "org-ins"),
        ("rep", "代表", "representative", "org-rep"),
        ("aud", "审计", "auditor", "org-audit"),
    ]
    for i, (pid, name, role, org) in enumerate(people):
        service.register_participant(request_id=f"pp{i}", actor_id="op", participant_id=pid,
                                     display_name=name, role=role, organization_id=org)
    service.register_artwork(request_id="a1", actor_id="op", artwork_id="art",
                             title="作品", representative_id="rep", components=["主机", "配件"])
    resources = [
        ("wsh", "venue_window", "vsh", "上海窗",
         {"city": "上海", "window_start": "2026-11-01T00:00:00Z",
          "window_end": "2026-12-31T00:00:00Z"}),
        ("wsh2", "venue_window", "vsh", "上海备用窗",
         {"city": "上海", "window_start": "2026-11-01T00:00:00Z",
          "window_end": "2026-12-31T00:00:00Z"}),
        ("wbj", "venue_window", "vbj", "北京窗",
         {"city": "北京", "window_start": "2026-11-01T00:00:00Z",
          "window_end": "2026-12-15T00:00:00Z"}),
        ("wbj2", "venue_window", "vbj2", "北京备用窗",
         {"city": "北京", "window_start": "2026-11-01T00:00:00Z",
          "window_end": "2026-12-31T00:00:00Z"}),
        ("csh", "display_case", "vsh", "上海柜", {"city": "上海"}),
        ("cbj", "display_case", "vbj", "北京柜", {"city": "北京"}),
        ("cbj2", "display_case", "vbj2", "备用北京柜", {"city": "北京"}),
        ("t1", "vehicle", "car", "车1", {"capacity": 10}),
        ("t2", "vehicle", "car", "车2", {"capacity": 12}),
        ("q1", "insurance_quota", "ins", "额度1", {"capacity": 1000000, "currency": "CNY"}),
    ]
    for i, (rid, kind, owner, label, caps) in enumerate(resources):
        service.register_resource(request_id=f"r{i}", actor_id="op", resource_id=rid,
                                  kind=kind, owner_id=owner, label=label, capabilities=caps)
    for i, org in enumerate(("org-sh", "org-bj", "org-bj2")):
        service.upsert_qualification(request_id=f"q{i}", actor_id="op",
                                     venue_organization_id=org, artwork_id="art", qualified=True)


def _segments(venue_bj="wbj", case_bj="cbj", bj_end="2026-11-20T20:00:00Z",
              insurance_end="2026-11-25T00:00:00Z"):
    return [
        {"segment_id": "s-sh", "kind": "venue",
         "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-12T20:00:00Z",
         "resource_id": "wsh",
         "requirement": {"case_resource_id": "csh", "install_hours": 4, "dismantle_hours": 4},
         "alternatives": ["wsh2"]},
        {"segment_id": "s-tr", "kind": "transport",
         "start_at": "2026-11-13T00:00:00Z", "end_at": "2026-11-14T00:00:00Z",
         "resource_id": "t1",
         "requirement": {"from_city": "上海", "to_city": "北京", "load_volume": 6},
         "alternatives": ["t2"]},
        {"segment_id": "s-bj", "kind": "venue",
         "start_at": "2026-11-15T08:00:00Z", "end_at": bj_end,
         "resource_id": venue_bj,
         "requirement": {"case_resource_id": case_bj, "install_hours": 4, "dismantle_hours": 4},
         "alternatives": ["wbj2"]},
        {"segment_id": "s-ins", "kind": "insurance",
         "start_at": "2026-11-10T08:00:00Z", "end_at": insurance_end,
         "resource_id": "q1",
         "requirement": {"coverage_amount": 500000, "currency": "CNY"}},
    ]


SESSIONS = {
    "s-sh": [{"starts_at": "2026-11-11T10:00:00Z", "label": "沪场", "capacity": 100}],
    "s-bj": [{"starts_at": "2026-11-16T10:00:00Z", "label": "京场", "capacity": 100}],
}


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "tour.sqlite3")
        self.database = Database(self.db_path)
        self.clock = FixedClock(datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.service = TourScheduler(self.database, self.clock)
        _bootstrap(self.service)
        self.service.create_plan_version(
            request_id="plan1", actor_id="op", plan_id="tour", artwork_id="art",
            components=["主机", "配件"], segments=_segments(),
            sessions=SESSIONS, lease_minutes=600)

    def tearDown(self):
        self.database.close()
        self._tmp.cleanup()

    def _confirm_and_publish(self):
        for i, (actor, seg) in enumerate([
                ("vsh", "s-sh"), ("car", "s-tr"), ("vbj", "s-bj"), ("ins", "s-ins")]):
            self.service.respond(request_id=f"ok{i}", actor_id=actor, plan_id="tour", version=1,
                                 segment_id=seg, decision="confirm")
        for i, seg in enumerate(("s-sh", "s-tr", "s-bj", "s-ins")):
            self.service.respond(request_id=f"rep{i}", actor_id="rep", plan_id="tour", version=1,
                                 segment_id=seg, decision="confirm")
        self.service.publish(request_id="pub", actor_id="op", plan_id="tour")

    def test_unqualified_venue_rejected(self):
        self.service.upsert_qualification(request_id="qoff", actor_id="op",
                                          venue_organization_id="org-bj2", artwork_id="art",
                                          qualified=False, certificate="")
        with self.assertRaises(ValidationError):
            self.service.create_plan_version(
                request_id="planX", actor_id="op", plan_id="tourX", artwork_id="art",
                components=["主机"], segments=_segments(venue_bj="wbj2", case_bj="cbj2"),
                sessions={k: v for k, v in SESSIONS.items()}, lease_minutes=600)

    def test_double_booking_reports_blocker(self):
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_plan_version(
                request_id="plan2", actor_id="op", plan_id="tour2", artwork_id="art",
                components=["主机"], segments=[
                    {"segment_id": "x1", "kind": "venue",
                     "start_at": "2026-11-10T09:00:00Z", "end_at": "2026-11-12T12:00:00Z",
                     "resource_id": "wsh",
                     "requirement": {"case_resource_id": "csh", "install_hours": 2,
                                     "dismantle_hours": 2}},
                    {"segment_id": "x2", "kind": "insurance",
                     "start_at": "2026-11-10T09:00:00Z", "end_at": "2026-11-12T12:00:00Z",
                     "resource_id": "q1",
                     "requirement": {"coverage_amount": 500000, "currency": "CNY"}},
                ],
                sessions={"x1": [{"starts_at": "2026-11-11T10:00:00Z",
                                  "label": "撞", "capacity": 1}]})
        blockers = getattr(ctx.exception, "blockers", [])
        self.assertTrue(any(b["resource_id"] == "wsh" for b in blockers))

    def test_insurance_quota_is_additive(self):
        with self.assertRaises(ConflictError):
            self.service.create_plan_version(
                request_id="plan3", actor_id="op", plan_id="tour3", artwork_id="art",
                components=["主机"], segments=[
                    {"segment_id": "y1", "kind": "venue",
                     "start_at": "2026-11-18T08:00:00Z", "end_at": "2026-11-19T20:00:00Z",
                     "resource_id": "wbj2",
                     "requirement": {"case_resource_id": "cbj2", "install_hours": 2,
                                     "dismantle_hours": 2}},
                    {"segment_id": "y2", "kind": "insurance",
                     "start_at": "2026-11-11T00:00:00Z", "end_at": "2026-11-20T00:00:00Z",
                     "resource_id": "q1",
                     "requirement": {"coverage_amount": 800000, "currency": "CNY"}},
                ],
                sessions={"y1": [{"starts_at": "2026-11-18T10:00:00Z",
                                  "label": "额满", "capacity": 1}]})

    def test_repeated_confirm_is_idempotent_and_holds_no_more(self):
        first = self.service.respond(request_id="cf1", actor_id="vsh", plan_id="tour", version=1,
                                     segment_id="s-sh", decision="confirm")
        second = self.service.respond(request_id="cf1", actor_id="vsh", plan_id="tour", version=1,
                                      segment_id="s-sh", decision="confirm")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        count = self.database.connection.execute(
            "SELECT COUNT(*) FROM resource_holds WHERE segment_id='s-sh'").fetchone()[0]
        self.assertEqual(2, count)  # 窗口 + 展柜各一条，重复确认不新增

    def test_other_organization_cannot_confirm(self):
        with self.assertRaises(PermissionDenied):
            self.service.respond(request_id="cfx", actor_id="vbj2", plan_id="tour", version=1,
                                 segment_id="s-sh", decision="confirm")

    def test_publish_requires_all_confirmations(self):
        self.service.respond(request_id="z1", actor_id="vsh", plan_id="tour", version=1,
                             segment_id="s-sh", decision="confirm")
        with self.assertRaises(StateError):
            self.service.publish(request_id="pubx", actor_id="op", plan_id="tour")

    def test_rejection_only_frees_draft_and_requires_rework(self):
        self.service.respond(request_id="rj", actor_id="vbj", plan_id="tour", version=1,
                             segment_id="s-bj", decision="reject", note="展厅检修")
        plan = self.service.get_plan(actor_id="op", plan_id="tour")
        self.assertEqual("rejected", plan["versions"][0]["status"])
        # 草案租约全部释放，资源可被重新预订。
        held = self.database.connection.execute(
            "SELECT COUNT(*) FROM resource_holds WHERE plan_id='tour' AND version=1"
        ).fetchone()[0]
        self.assertEqual(0, held)
        with self.assertRaises(StateError):
            self.service.publish(request_id="pubbad", actor_id="op", plan_id="tour")

    def test_concurrent_publish_single_winner(self):
        import tempfile
        from pathlib import Path
        for i, (actor, seg) in enumerate([
                ("vsh", "s-sh"), ("car", "s-tr"), ("vbj", "s-bj"), ("ins", "s-ins")]):
            self.service.respond(request_id=f"cc{i}", actor_id=actor, plan_id="tour", version=1,
                                 segment_id=seg, decision="confirm")
        for i, seg in enumerate(("s-sh", "s-tr", "s-bj", "s-ins")):
            self.service.respond(request_id=f"cr{i}", actor_id="rep", plan_id="tour", version=1,
                                 segment_id=seg, decision="confirm")
        # 两个独立连接指向同一文件库并发发布：BEGIN IMMEDIATE 串行化，最多一个生效。
        db2 = Database(self.db_path)
        service2 = TourScheduler(db2, self.clock)
        results = []
        barrier = threading.Barrier(2)

        def publish(service, tag):
            barrier.wait()
            try:
                service.publish(request_id=f"pub-{tag}", actor_id="op", plan_id="tour")
                results.append("ok")
            except StateError:
                results.append("state")

        threads = [
            threading.Thread(target=publish, args=(self.service, "a")),
            threading.Thread(target=publish, args=(service2, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        db2.close()
        self.assertEqual(["ok", "state"], sorted(results))
        plan = self.service.get_plan(actor_id="op", plan_id="tour")
        self.assertEqual(1, plan["published_version"])

    def test_handover_blocks_silent_overwrite_and_rework(self):
        self._confirm_and_publish()
        self.service.record_handover(request_id="h1", actor_id="vsh", plan_id="tour",
                                     segment_id="s-sh", handover_ref="DN-1")
        with self.assertRaises(StateError):
            self.service.record_handover(request_id="h2", actor_id="vsh", plan_id="tour",
                                         segment_id="s-sh", handover_ref="DN-2")
        with self.assertRaises(ValidationError):
            self.service.rework_plan(
                request_id="rwbad", actor_id="op", plan_id="tour", reason="venue_closed",
                replacements=[{"replaces_segment_id": "s-sh", "segment_id": "s-sh2",
                               "start_at": "2026-12-01T08:00:00Z",
                               "end_at": "2026-12-02T20:00:00Z",
                               "requirement": {"install_hours": 2, "dismantle_hours": 2}}],
                sessions={"s-sh2": [{"starts_at": "2026-12-01T10:00:00Z",
                                     "label": "新沪场", "capacity": 10}]})

    def test_window_shortening_uses_stable_fallback_and_preserves_rest(self):
        self._confirm_and_publish()
        self.service.record_handover(request_id="hsh", actor_id="vsh", plan_id="tour",
                                     segment_id="s-sh", handover_ref="DN-SH")
        self.service.record_cost(request_id="cost", actor_id="op", plan_id="tour",
                                 segment_id="s-bj", amount="800", currency="CNY",
                                 category="宣传费")
        self.clock.advance(days=2)
        self.service.rework_plan(
            request_id="rw1", actor_id="op", plan_id="tour", reason="window_shortened",
            replacements=[{"replaces_segment_id": "s-bj", "segment_id": "s-bj2",
                           "start_at": "2026-11-21T08:00:00Z", "end_at": "2026-11-23T20:00:00Z",
                           "requirement": {"install_hours": 4, "dismantle_hours": 4},
                           "alternatives": ["wbj2", "wbj"]}],
            sessions={"s-bj2": [{"starts_at": "2026-11-22T10:00:00Z",
                                 "label": "京场改期", "capacity": 120}]})
        plan = self.service.get_plan(actor_id="op", plan_id="tour")
        v2 = {s["segment_id"]: s for s in plan["versions"][1]["segments"]}
        self.assertEqual("wbj2", v2["s-bj2"]["resource_id"])
        self.assertEqual("cbj2", v2["s-bj2"]["requirement"]["case_resource_id"])
        self.assertEqual("handed_over", v2["s-sh"]["status"])
        self.assertEqual("DN-SH", v2["s-sh"]["handover_ref"])
        # 未受影响的运输区段不需要承运方重新确认。
        self.assertEqual([], self.service.list_todos(actor_id="car"))
        # 受影响费用前向关联但金额不变。
        carried = self.database.connection.execute(
            "SELECT amount,carried_into_version FROM incurred_costs WHERE segment_id='s-bj'"
        ).fetchone()
        self.assertEqual(("800.00", 2), tuple(carried))

    def test_transport_delay_rework_only_replaces_transport(self):
        self._confirm_and_publish()
        self.service.record_handover(request_id="hsh2", actor_id="vsh", plan_id="tour",
                                     segment_id="s-sh", handover_ref="DN-SH2")
        self.service.rework_plan(
            request_id="rw2", actor_id="op", plan_id="tour", reason="transport_delay",
            replacements=[{"replaces_segment_id": "s-tr", "segment_id": "s-tr2",
                           "start_at": "2026-11-13T06:00:00Z", "end_at": "2026-11-14T06:00:00Z",
                           "resource_id": "t2",
                           "requirement": {"from_city": "上海", "to_city": "北京",
                                           "load_volume": 6}}])
        todos = {(t["role"], t["segment_id"]) for t in self.service.list_todos(actor_id="op")}
        self.assertIn(("carrier", "s-tr2"), todos)
        self.assertNotIn(("venue", "s-sh"), todos)

    def test_lease_expiry_releases_holds_but_keeps_published(self):
        self._confirm_and_publish()
        self.service.create_plan_version(
            request_id="plandraft", actor_id="op", plan_id="tour-draft", artwork_id="art",
            components=["主机"], segments=[
                {"segment_id": "d1", "kind": "venue",
                 "start_at": "2026-12-10T08:00:00Z", "end_at": "2026-12-11T20:00:00Z",
                 "resource_id": "wsh2",
                 "requirement": {"case_resource_id": "csh", "install_hours": 2,
                                 "dismantle_hours": 2}},
                {"segment_id": "d2", "kind": "insurance",
                 "start_at": "2026-12-10T08:00:00Z", "end_at": "2026-12-11T20:00:00Z",
                 "resource_id": "q1",
                 "requirement": {"coverage_amount": 1000, "currency": "CNY"}},
            ],
            sessions={"d1": [{"starts_at": "2026-12-10T10:00:00Z",
                              "label": "草案场", "capacity": 1}]},
            lease_minutes=30)
        self.clock.advance(minutes=31)
        swept = self.service.sweep_expired()
        self.assertIn("tour-draft/v1", swept["expired"])
        held = self.database.connection.execute(
            "SELECT COUNT(*) FROM resource_holds WHERE plan_id='tour-draft'"
        ).fetchone()[0]
        self.assertEqual(0, held)
        self.assertEqual(1, self.service.get_plan(actor_id="op", plan_id="tour")
                         ["published_version"])

    def test_abort_rejected_version_releases_retained_holds(self):
        self.service.respond(request_id="ab1", actor_id="vsh", plan_id="tour", version=1,
                             segment_id="s-sh", decision="confirm")
        self.service.respond(request_id="ab2", actor_id="rep", plan_id="tour", version=1,
                             segment_id="s-sh", decision="confirm")
        self.service.respond(request_id="ab3", actor_id="vbj", plan_id="tour", version=1,
                             segment_id="s-bj", decision="reject", note="检修")
        self.service.abort_version(request_id="abort1", actor_id="op", plan_id="tour",
                                   reason="停止巡展计划")
        held = self.database.connection.execute(
            "SELECT COUNT(*) FROM resource_holds WHERE plan_id='tour'"
        ).fetchone()[0]
        self.assertEqual(0, held)
        # 释放后窗口可被新草案预订。
        receipt = self.service.create_plan_version(
            request_id="newafterabort", actor_id="op", plan_id="tour-new", artwork_id="art",
            components=["主机"], segments=[
                {"segment_id": "n1", "kind": "venue",
                 "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-12T20:00:00Z",
                 "resource_id": "wsh",
                 "requirement": {"case_resource_id": "csh", "install_hours": 4,
                                 "dismantle_hours": 4}},
                {"segment_id": "n2", "kind": "insurance",
                 "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-12T20:00:00Z",
                 "resource_id": "q1",
                 "requirement": {"coverage_amount": 100, "currency": "CNY"}},
            ],
            sessions={"n1": [{"starts_at": "2026-11-11T10:00:00Z", "label": "新场",
                              "capacity": 10}]})
        self.assertFalse(receipt.replayed)

    def test_rejection_then_rework_preserves_confirmed_segments(self):
        # 上海场馆与代表已确认上海区段；随后北京场馆拒绝北京区段。
        self.service.respond(request_id="rr1", actor_id="vsh", plan_id="tour", version=1,
                             segment_id="s-sh", decision="confirm")
        self.service.respond(request_id="rr2", actor_id="rep", plan_id="tour", version=1,
                             segment_id="s-sh", decision="confirm")
        self.service.respond(request_id="rr3", actor_id="vbj", plan_id="tour", version=1,
                             segment_id="s-bj", decision="reject", note="展厅检修")
        # 已确认区段的租约保留，未确认区段（含被拒）释放。
        retained = self.database.connection.execute(
            "SELECT segment_id FROM resource_holds WHERE plan_id='tour' AND version=1"
        ).fetchall()
        self.assertEqual({"s-sh"}, {r["segment_id"] for r in retained})

        # 只重排被拒的北京区段；上海/运输/保险按各自确认状态处理。
        self.service.rework_plan(
            request_id="rwrej", actor_id="op", plan_id="tour", reason="rejection",
            replacements=[{"replaces_segment_id": "s-bj", "segment_id": "s-bj-new",
                           "start_at": "2026-11-15T08:00:00Z", "end_at": "2026-11-17T20:00:00Z",
                           "requirement": {"install_hours": 4, "dismantle_hours": 4},
                           "alternatives": ["wbj2", "wbj"]}],
            sessions={"s-bj-new": [{"starts_at": "2026-11-16T10:00:00Z",
                                    "label": "北京新场", "capacity": 90}]})
        # 已确认的上海区段无需任何一方重复确认。
        self.assertEqual([], self.service.list_todos(actor_id="vsh"))
        pending = {(t["role"], t["segment_id"]) for t in self.service.list_todos(actor_id="op")}
        self.assertNotIn(("venue", "s-sh"), pending)
        self.assertIn(("venue", "s-bj-new"), pending)
        self.assertIn(("carrier", "s-tr"), pending)
        self.assertIn(("insurer", "s-ins"), pending)
        for req_id, (actor, seg) in enumerate([
                ("car", "s-tr"), ("ins", "s-ins"), ("vbj2", "s-bj-new")]):
            self.service.respond(request_id=f"n{req_id}", actor_id=actor, plan_id="tour",
                                 version=2, segment_id=seg, decision="confirm")
        for req_id, seg in enumerate(("s-tr", "s-ins", "s-bj-new")):
            self.service.respond(request_id=f"nr{req_id}", actor_id="rep", plan_id="tour",
                                 version=2, segment_id=seg, decision="confirm")
        self.service.publish(request_id="npub", actor_id="op", plan_id="tour")
        plan = self.service.get_plan(actor_id="op", plan_id="tour")
        v2 = {s["segment_id"]: s for s in plan["versions"][1]["segments"]}
        self.assertEqual("wbj2", v2["s-bj-new"]["resource_id"])
        self.assertEqual("s-sh", next(s for s in plan["versions"][1]["segments"]
                                      if s["segment_id"] == "s-sh")["segment_id"])
        # 上海区段的原场次随携带继续公布。
        sh_session = v2["s-sh"]["sessions"][0]
        self.assertEqual("announced", sh_session["publicity_state"])

    def test_venue_closed_falls_back_by_priority_list(self):
        self._confirm_and_publish()
        # 临时闭馆：首选 wbj2 被另一草案占用时，按优先级顺序落到仍可用的 wbj 不行——
        # wbj 即闭馆场馆本身；因此再放一个无关北京窗口 wbj3 验证稳定次序。
        self.service.register_resource(
            request_id="rwbj3", actor_id="op", resource_id="wbj3", kind="venue_window",
            owner_id="vbj2", label="北京第三窗",
            capabilities={"city": "北京", "window_start": "2026-11-01T00:00:00Z",
                          "window_end": "2026-12-31T00:00:00Z"})
        self.service.register_resource(
            request_id="rcbj3", actor_id="op", resource_id="cbj3", kind="display_case",
            owner_id="vbj2", label="第三窗展柜", capabilities={"city": "北京"})
        self.service.create_plan_version(
            request_id="blockplan", actor_id="op", plan_id="tour-block", artwork_id="art",
            components=["主机"], segments=[
                {"segment_id": "k1", "kind": "venue",
                 "start_at": "2026-11-21T08:00:00Z", "end_at": "2026-11-23T20:00:00Z",
                 "resource_id": "wbj2",
                 "requirement": {"case_resource_id": "cbj2", "install_hours": 2,
                                 "dismantle_hours": 2}},
                {"segment_id": "k2", "kind": "insurance",
                 "start_at": "2026-11-21T08:00:00Z", "end_at": "2026-11-23T20:00:00Z",
                 "resource_id": "q1",
                 "requirement": {"coverage_amount": 100, "currency": "CNY"}},
            ],
            sessions={"k1": [{"starts_at": "2026-11-22T10:00:00Z",
                              "label": "占位", "capacity": 1}]})
        self.service.rework_plan(
            request_id="rwclosed", actor_id="op", plan_id="tour", reason="venue_closed",
            replacements=[{"replaces_segment_id": "s-bj", "segment_id": "s-bj-closed",
                           "start_at": "2026-11-21T08:00:00Z", "end_at": "2026-11-23T20:00:00Z",
                           "requirement": {"install_hours": 4, "dismantle_hours": 4},
                           "prefer_resources": ["wbj2", "wbj3"]}],
            sessions={"s-bj-closed": [{"starts_at": "2026-11-22T10:00:00Z",
                                       "label": "闭馆改期", "capacity": 100}]})
        plan = self.service.get_plan(actor_id="op", plan_id="tour")
        v2 = {s["segment_id"]: s for s in plan["versions"][1]["segments"]}
        self.assertEqual("wbj3", v2["s-bj-closed"]["resource_id"])
        self.assertEqual("cbj3", v2["s-bj-closed"]["requirement"]["case_resource_id"])

    def test_damage_reason_supported(self):
        self._confirm_and_publish()
        receipt = self.service.rework_plan(
            request_id="rwdmg", actor_id="op", plan_id="tour", reason="damage",
            replacements=[{"replaces_segment_id": "s-bj", "segment_id": "s-bj-dmg",
                           "start_at": "2026-11-16T08:00:00Z", "end_at": "2026-11-18T20:00:00Z",
                           "requirement": {"install_hours": 4, "dismantle_hours": 4},
                           "alternatives": ["wbj2", "wbj"]}],
            sessions={"s-bj-dmg": [{"starts_at": "2026-11-17T10:00:00Z",
                                    "label": "损伤改期", "capacity": 80}]})
        self.assertFalse(receipt.replayed)

    def test_snapshot_reconstructs_responsibility_chain(self):
        self._confirm_and_publish()
        self.service.record_handover(request_id="hh", actor_id="vsh", plan_id="tour",
                                     segment_id="s-sh", handover_ref="DN-X")
        snapshot = self.service.snapshot_at(actor_id="aud", plan_id="tour",
                                            at="2026-11-02T00:00:00Z")
        v1 = snapshot["versions"][0]
        self.assertEqual("published", v1["status_as_of"])
        self.assertEqual(["s-sh"], v1["handed_over_segments"])
        actors = {e["actor_id"] for e in v1["timeline"]}
        self.assertIn("vsh", actors)


if __name__ == "__main__":
    unittest.main()
