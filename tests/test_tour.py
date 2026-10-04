"""巡展调度服务的规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError, DomainError, PermissionDenied)
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database
from creative_program_foundation.tour import TourService

T0 = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)


def ts(day: int, hour: int = 0) -> str:
    return (T0 + timedelta(days=day, hours=hour)).isoformat().replace("+00:00", "Z")


class TourFixture:
    """构建一套包含主选/备用渠道资源的巡展环境。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.database = Database(path)
        self.clock = FixedClock(T0)
        self.svc = DomainService(self.database, self.clock)
        self.tour = TourService(self.database, self.clock)
        self._bootstrap()
        self._artwork()
        self._venues()
        self._carriers()
        self._insurance()

    def close(self) -> None:
        self.database.close()

    def advance(self, seconds: int) -> None:
        self.clock._value = self.clock.now() + timedelta(seconds=seconds)

    # ------------------------------------------------------------ 建档

    def _bootstrap(self) -> None:
        self.svc.register_organization(request_id="org", actor_id="bootstrap",
                                       organization_id="o1", name="机构")
        actors = [
            ("a1", "管理员", "admin"), ("op1", "运营", "operator"),
            ("rep1", "作品代表", "reviewer"), ("ins1", "保险经办", "insurance"),
            ("venue1", "商场联系人", "venue"), ("venue2", "博物馆联系人", "venue"),
            ("recv1", "接收人甲", "receiver"), ("recv2", "接收人乙", "receiver"),
            ("carrier1", "承运方甲", "carrier"), ("carrier2", "承运方乙", "carrier"),
        ]
        for i, (actor_id, name, role) in enumerate(actors):
            self.svc.register_actor(request_id=f"actor-{actor_id}", actor_id="bootstrap" if i == 0 else "a1",
                                    new_actor_id=actor_id, display_name=name,
                                    role=role, organization_id="o1")
        self.svc.register_site(request_id="site", actor_id="op1", site_id="s1",
                               organization_id="o1", name="节点", timezone_name="Asia/Shanghai")

    def _artwork(self) -> None:
        self.tour.register_artwork(
            request_id="aw1", actor_id="op1", artwork_id="art1", title="金奖作品",
            representative_id="rep1", declared_value=500000,
            spec={"pieces": 2, "weight_kg": 80, "volume_m3": 2,
                  "install_hours": 6, "climate": True, "security": True})

    def _venues(self) -> None:
        for vid, contact, name in (("venueA", "venue1", "商场"), ("venueB", "venue2", "博物馆")):
            self.tour.register_venue(request_id=f"v-{vid}", actor_id="op1", venue_id=vid,
                                     site_id="s1", name=name, contact_actor_id=contact,
                                     required_qualification="fine_art_level2")
        for d in (10, 30):
            for suffix, vid in (("a", "venueA"), ("b", "venueB")):
                self.tour.register_venue_window(
                    request_id=f"w-{suffix}{d}", actor_id="op1", window_id=f"win-{suffix}{d}",
                    venue_id=vid, starts_at=ts(d), ends_at=ts(d + 6))
                self.tour.register_display_case(
                    request_id=f"c-{suffix}{d}", actor_id="op1", case_id=f"case-{suffix}{d}",
                    venue_id=vid, label=f"{vid}展柜",
                    conditions={"max_weight_kg": 500, "max_volume_m3": 20,
                                "climate": True, "security": True})
                self.tour.register_labor(
                    request_id=f"l-{suffix}{d}", actor_id="op1", labor_id=f"labor-{suffix}{d}",
                    venue_id=vid, starts_at=ts(d), ends_at=ts(d + 6), available_hours=40)
        self.tour.register_receiver(request_id="r1", actor_id="op1", receiver_id="recv-a",
                                    venue_id="venueA", receiver_actor_id="recv1",
                                    qualification="fine_art_level2")
        self.tour.register_receiver(request_id="r2", actor_id="op1", receiver_id="recv-b",
                                    venue_id="venueB", receiver_actor_id="recv2",
                                    qualification="fine_art_level2")

    def _carriers(self) -> None:
        for cid, contact in (("carA", "carrier1"), ("carB", "carrier2")):
            self.tour.register_carrier(request_id=f"cr-{cid}", actor_id="op1", carrier_id=cid,
                                       name=cid, contact_actor_id=contact)
        for tag, cid, d0, d1 in (("s1a", "carA", 7, 10), ("s1b", "carB", 7, 10),
                                 ("s2a", "carA", 14, 32), ("s2b", "carB", 14, 32)):
            self.tour.register_carrier_slot(
                request_id=f"cs-{tag}", actor_id="op1", slot_id=f"slot-{tag}", carrier_id=cid,
                starts_at=ts(d0), ends_at=ts(d1),
                capacity={"max_weight_kg": 500, "max_volume_m3": 20, "max_pieces": 10})

    def _insurance(self) -> None:
        self.tour.register_insurance_policy(
            request_id="pol1", actor_id="op1", policy_id="ins-pol-1", name="全程艺术品险",
            handler_actor_id="ins1", limit_amount=2000000, starts_at=ts(5), ends_at=ts(40))

    # ------------------------------------------------------------ 清单与流程

    def manifest(self, plan_id: str = "tour-001", *, announce: bool = True) -> dict:
        return {
            "plan_id": plan_id,
            "artwork_ids": ["art1"],
            "rep_actor_id": "rep1",
            "lease_seconds": 600,
            "stops": [
                {"stop_key": "stop1",
                 "options": [
                     {"venue_id": "venueA", "window_id": "win-a10", "case_id": "case-a10",
                      "labor_id": "labor-a10", "receiver_id": "recv-a",
                      "install_at": ts(10, 2), "open_at": ts(10, 6),
                      "close_at": ts(13, 20), "dismantle_at": ts(14, 2), "cost": 3000},
                     {"venue_id": "venueB", "window_id": "win-b10", "case_id": "case-b10",
                      "labor_id": "labor-b10", "receiver_id": "recv-b",
                      "install_at": ts(10, 2), "open_at": ts(10, 6),
                      "close_at": ts(13, 20), "dismantle_at": ts(14, 2), "cost": 4000}],
                 "sessions": [{"session_key": "ss1", "title": "开幕式导览",
                               "starts_at": ts(10, 8), "ends_at": ts(10, 10), "announce": announce}],
                 "publicity": {"commitment": "开幕前 3 天发布媒体排期", "deadline": ts(8)}},
                {"stop_key": "stop2",
                 "options": [
                     {"venue_id": "venueA", "window_id": "win-a30", "case_id": "case-a30",
                      "labor_id": "labor-a30", "receiver_id": "recv-a",
                      "install_at": ts(30, 2), "open_at": ts(30, 6),
                      "close_at": ts(33, 20), "dismantle_at": ts(34, 2), "cost": 3000},
                     {"venue_id": "venueB", "window_id": "win-b30", "case_id": "case-b30",
                      "labor_id": "labor-b30", "receiver_id": "recv-b",
                      "install_at": ts(30, 2), "open_at": ts(30, 6),
                      "close_at": ts(33, 20), "dismantle_at": ts(34, 2), "cost": 4000}],
                 "sessions": [{"session_key": "ss2", "title": "茶会专场",
                               "starts_at": ts(31, 8), "ends_at": ts(31, 10), "announce": announce}]}],
            "segments": [
                {"segment_key": "seg1", "from_stop_key": None, "to_stop_key": "stop1",
                 "options": [
                     {"carrier_id": "carA", "slot_id": "slot-s1a",
                      "pickup_at": ts(8), "deliver_at": ts(9, 12), "cost": 1000},
                     {"carrier_id": "carB", "slot_id": "slot-s1b",
                      "pickup_at": ts(8), "deliver_at": ts(9, 12), "cost": 1500}]},
                {"segment_key": "seg2", "from_stop_key": "stop1", "to_stop_key": "stop2",
                 "options": [
                     {"carrier_id": "carA", "slot_id": "slot-s2a",
                      "pickup_at": ts(15), "deliver_at": ts(29, 12), "cost": 1000},
                     {"carrier_id": "carB", "slot_id": "slot-s2b",
                      "pickup_at": ts(15), "deliver_at": ts(29, 12), "cost": 1500}]}],
            "insurance": [
                {"policy_id": "ins-pol-1", "artwork_ids": ["art1"],
                 "cover_from": ts(7), "cover_to": ts(32), "cost": 800}]}

    def confirm_all(self, plan_id: str = "tour-001", version: int = 1, *,
                    parties=("rep1", "ins1", "venue1", "carrier1")) -> None:
        for i, actor_id in enumerate(parties):
            self.tour.confirm(request_id=f"cf-{plan_id}-{version}-{i}", actor_id=actor_id,
                              plan_id=plan_id, version=version)

    def publish_plan(self, plan_id: str = "tour-001", version: int = 1,
                     req: str = "pub") -> dict:
        return self.tour.publish(request_id=f"{req}-{plan_id}-{version}", actor_id="op1",
                                 plan_id=plan_id, version=version)

    def draft_and_publish(self, plan_id: str = "tour-001") -> dict:
        self.tour.create_draft(request_id=f"draft-{plan_id}", actor_id="op1",
                               manifest=self.manifest(plan_id))
        self.confirm_all(plan_id, 1)
        return self.publish_plan(plan_id, 1)


class HappyPathTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TourFixture()

    def tearDown(self):
        self.ctx.close()

    def test_draft_confirmation_publish_happy_path(self):
        result = self.ctx.draft_and_publish()
        self.assertEqual("published", result["status"])
        plan = self.ctx.tour.get_plan("tour-001")
        self.assertEqual(1, plan["effective_version"])

    def test_repeated_confirmation_does_not_extend_lease_or_add_allocations(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        ctx.advance(300)
        ctx.tour.confirm(request_id="cf", actor_id="rep1", plan_id="tour-001", version=1)
        before = ctx.database.connection.execute(
            "SELECT COUNT(*) c, MIN(lease_expires_at) m FROM resource_allocations "
            "WHERE plan_id='tour-001' AND version=1").fetchone()
        # 用新请求号重复确认：返回 duplicate，不续租
        ctx.advance(100)
        duplicate = ctx.tour.confirm(request_id="cf-dup", actor_id="rep1",
                                     plan_id="tour-001", version=1)
        self.assertTrue(duplicate["duplicate"])
        after = ctx.database.connection.execute(
            "SELECT COUNT(*) c, MIN(lease_expires_at) m FROM resource_allocations "
            "WHERE plan_id='tour-001' AND version=1").fetchone()
        self.assertEqual(before["c"], after["c"])
        self.assertEqual(before["m"], after["m"])

    def test_replay_same_request_id_returns_original_receipt(self):
        ctx = self.ctx
        first = ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        second = ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["version"], second["version"])

    def test_confirmation_order_is_enforced(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        with self.assertRaises(ConflictError):
            ctx.tour.confirm(request_id="x", actor_id="ins1", plan_id="tour-001", version=1)

    def test_publish_requires_all_confirmations(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        with self.assertRaises(ConflictError):
            ctx.publish_plan()

    def test_rejection_releases_resources(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        ctx.tour.confirm(request_id="cf", actor_id="rep1", plan_id="tour-001",
                         version=1, approved=False, comment="作品保险条款不接受")
        held = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM resource_allocations WHERE plan_id='tour-001' "
            "AND version=1 AND status='held'").fetchone()["c"]
        self.assertEqual(0, held)
        # 被拒后同一资源可以被新草案占用
        other = ctx.manifest("tour-009")
        other["insurance"][0]["cost"] = 0
        draft = ctx.tour.create_draft(request_id="d2", actor_id="op1", manifest=other)
        self.assertEqual("draft", draft["status"])


class ConflictAndLeaseTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TourFixture()

    def tearDown(self):
        self.ctx.close()

    def test_double_booking_is_blocked_with_recovery_hints(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        blocked = ctx.tour.create_draft(request_id="d2", actor_id="op1",
                                        manifest=ctx.manifest("tour-002"))
        self.assertEqual("blocked", blocked["status"])
        codes = {item["code"] for item in blocked["conflicts"]}
        self.assertIn("double_booking", codes)
        hints = blocked["conflicts"][0]["recovery"]
        self.assertTrue(any("备用" in h for h in hints))
        conflict = ctx.tour.get_conflicts("tour-002")
        self.assertEqual("resource_conflict", conflict["items"][0]["detail"]["kind"])

    def test_expired_lease_releases_resources(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        ctx.advance(601)
        other = ctx.manifest("tour-003")
        draft = ctx.tour.create_draft(request_id="d3", actor_id="op1", manifest=other)
        self.assertEqual("draft", draft["status"])
        expired = ctx.database.connection.execute(
            "SELECT status FROM tour_plans WHERE plan_id='tour-001' AND version=1").fetchone()["status"]
        self.assertEqual("blocked", expired)

    def test_insurance_limit_blocks_overlapping_plans(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        # 再登记一件高价值作品并让第二张计划占用剩余额度
        ctx.tour.register_artwork(
            request_id="aw2", actor_id="op1", artwork_id="art2", title="银奖作品",
            representative_id="rep1", declared_value=1_800_000,
            spec={"pieces": 1, "weight_kg": 10, "volume_m3": 1, "install_hours": 2})
        manifest = ctx.manifest("tour-004")
        manifest["artwork_ids"] = ["art2"]
        manifest["insurance"][0]["artwork_ids"] = ["art2"]
        blocked = ctx.tour.create_draft(request_id="d4", actor_id="op1", manifest=manifest)
        self.assertEqual("blocked", blocked["status"])
        self.assertTrue(any("投保额度" in item["reason"] for item in blocked["conflicts"]))

    def test_case_conditions_block_draft(self):
        ctx = self.ctx
        # 给 venueA 安装不具备恒温条件的展柜并引用
        ctx.tour.register_display_case(
            request_id="case-bad", actor_id="op1", case_id="case-bad10",
            venue_id="venueA", label="普通展柜",
            conditions={"max_weight_kg": 5, "max_volume_m3": 1,
                        "climate": False, "security": False})
        manifest = ctx.manifest("tour-005")
        manifest["stops"][0]["options"][0]["case_id"] = "case-bad10"
        blocked = ctx.tour.create_draft(request_id="d5", actor_id="op1", manifest=manifest)
        self.assertEqual("blocked", blocked["status"])
        self.assertTrue(any(item["code"].startswith("case_") for item in blocked["conflicts"]))

    def test_receiver_qualification_must_match_venue(self):
        ctx = self.ctx
        ctx.tour.register_receiver(request_id="r3", actor_id="op1", receiver_id="recv-c",
                                   venue_id="venueB", receiver_actor_id="recv1",
                                   qualification="basic_only")
        manifest = ctx.manifest("tour-006")
        manifest["stops"][0]["options"][1]["receiver_id"] = "recv-c"
        with self.assertRaises(DomainError):
            ctx.tour.create_draft(request_id="d6", actor_id="op1", manifest=manifest)


class RerouteTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TourFixture()
        self.ctx.draft_and_publish()
        # seg1 发运并完成交接（不可再被重排）
        self.ctx.tour.dispatch_segment(request_id="dp1", actor_id="carrier1",
                                       plan_id="tour-001", segment_key="seg1")
        self.ctx.tour.complete_handover(request_id="hd1", actor_id="recv1",
                                        plan_id="tour-001", segment_key="seg1",
                                        condition_note="完好")

    def tearDown(self):
        self.ctx.close()

    def _confirm_reroute(self, version: int, parties):
        for i, actor_id in enumerate(parties):
            self.ctx.tour.confirm(request_id=f"rcf-{version}-{i}", actor_id=actor_id,
                                  plan_id="tour-001", version=version)

    def test_venue_close_falls_back_by_stable_priority_and_preserves_handover(self):
        ctx = self.ctx
        ctx.advance(25 * 86400)
        result = ctx.tour.close_venue(request_id="close", actor_id="venue1",
                                      venue_id="venueA", reason="消防检修",
                                      notice_confirmed=True)
        reroute = result["reroutes"][0]
        self.assertEqual(2, reroute["version"])
        # 主选项标记 window_closed，按顺序启用 index=1 的备用场馆
        attempts = {(a["stop_key"], a["option_index"]): a["blockers"]
                    for a in reroute["attempts"]}
        self.assertEqual(["window_closed"], attempts[("stop1", 0)])
        self.assertEqual([], attempts[("stop1", 1)])
        # 已交接的 seg1 不在重排范围
        self.assertNotIn("seg1", reroute["changed_segments"])
        # 重排版本只需作品代表 + 备用场馆确认，承运方未变动不参与
        self._confirm_reroute(2, ("rep1", "venue2"))
        ctx.tour.publish(request_id="pub2", actor_id="op1", plan_id="tour-001", version=2)
        self.assertEqual(2, ctx.tour.get_plan("tour-001")["effective_version"])
        # 已完成交接记录原样保留
        seg1 = [s for s in ctx.tour.get_plan("tour-001")["segment_states"]
                if s["segment_key"] == "seg1"][0]
        self.assertEqual("delivered", seg1["state"])
        handovers = ctx.tour.restore_at("tour-001", ts(60))["handovers"]
        self.assertEqual(1, len(handovers))

    def test_incurred_costs_are_not_silently_overwritten(self):
        ctx = self.ctx
        before = ctx.tour.get_costs("tour-001")["total"]
        ctx.advance(25 * 86400)
        ctx.tour.close_venue(request_id="close", actor_id="venue1", venue_id="venueA",
                             reason="闭馆", notice_confirmed=True)
        self._confirm_reroute(2, ("rep1", "venue2"))
        ctx.tour.publish(request_id="pub2", actor_id="op1", plan_id="tour-001", version=2)
        after = ctx.tour.get_costs("tour-001")
        # 原场馆费用保留，只追加备用场馆费用
        self.assertGreater(after["total"], before)
        kinds = {(item["ref_kind"], item["ref_key"]) for item in after["items"]}
        self.assertIn(("stop", "win-a10"), kinds)  # 原商场费用保留
        self.assertIn(("stop", "win-b10"), kinds)  # 备用博物馆费用追加

    def test_window_shortening_reroutes_only_affected_stop(self):
        ctx = self.ctx
        ctx.advance(20 * 86400)
        result = ctx.tour.shorten_venue_window(
            request_id="short", actor_id="venue1", window_id="win-a10",
            new_starts_at=ts(11), new_ends_at=ts(12), reason="商场活动占用",
            notice_confirmed=True)
        reroute = result["reroutes"][0]
        self.assertEqual(["stop1"], reroute["changed_stops"])
        self.assertNotIn("stop2", reroute["changed_stops"])

    def test_transport_delay_shifts_downstream_and_protects_announced_sessions(self):
        ctx = self.ctx
        ctx.advance(16 * 86400)
        # seg2 延误 2 天：主选 slot 时间窗容不下，需要新时刻；此处直接给出可用时刻
        with self.assertRaises(ConflictError) as caught:
            ctx.tour.report_incident(
                request_id="delay", actor_id="carrier1", plan_id="tour-001",
                kind="transport_delay", segment_keys=["seg2"],
                detail={"new_pickup_at": ts(17), "new_deliver_at": ts(30, 6)})
        self.assertIn("公开排期", str(caught.exception))
        # 显式确认观众告知后通过
        reroute = ctx.tour.report_incident(
            request_id="delay", actor_id="carrier1", plan_id="tour-001",
            kind="transport_delay", segment_keys=["seg2"],
            detail={"new_pickup_at": ts(17), "new_deliver_at": ts(30, 6)},
            notice_confirmed=True)
        self.assertEqual(2, reroute["version"])
        self.assertIn("stop2", reroute["changed_stops"])
        self.assertTrue(reroute["session_impact"])
        # 已交接的 seg1 不能重排
        with self.assertRaises(ConflictError):
            ctx.tour.report_incident(
                request_id="damage", actor_id="carrier1", plan_id="tour-001",
                kind="artwork_damaged", segment_keys=["seg1"], detail={})

    def test_superseded_version_leaves_single_active_allocation_per_resource(self):
        ctx = self.ctx
        ctx.advance(25 * 86400)
        ctx.tour.close_venue(request_id="close", actor_id="venue1", venue_id="venueA",
                             reason="闭馆", notice_confirmed=True)
        self._confirm_reroute(2, ("rep1", "venue2"))
        ctx.tour.publish(request_id="pub2", actor_id="op1", plan_id="tour-001", version=2)
        rows = ctx.database.connection.execute(
            "SELECT resource_type, resource_id, COUNT(*) c FROM resource_allocations "
            "WHERE plan_id='tour-001' AND status='confirmed' GROUP BY resource_type, resource_id"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r["c"] == 1 for r in rows), [dict(r) for r in rows])
        old = ctx.database.connection.execute(
            "SELECT COUNT(*) c FROM resource_allocations WHERE plan_id='tour-001' "
            "AND version=1 AND status='confirmed'").fetchone()["c"]
        self.assertEqual(0, old)

    def test_manual_replan_uses_specified_backup(self):
        ctx = self.ctx
        ctx.advance(20 * 86400)
        reroute = ctx.tour.replan(
            request_id="manual", actor_id="op1", plan_id="tour-001",
            stop_overrides={"stop2": 1}, notice_confirmed=False)
        self.assertEqual(2, reroute["version"])
        manifest = ctx.database.connection.execute(
            "SELECT manifest_json FROM tour_plans WHERE plan_id='tour-001' AND version=2"
        ).fetchone()["manifest_json"]
        import json
        chosen = json.loads(manifest)["stops"][1]["chosen"]["venue_id"]
        self.assertEqual("venueB", chosen)


class TodosAndMaterialsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TourFixture()

    def tearDown(self):
        self.ctx.close()

    def test_each_party_sees_only_its_minimal_materials(self):
        ctx = self.ctx
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        rep_todo = ctx.tour.list_todos("rep1")[0]
        self.assertEqual("representative", rep_todo["party_kind"])
        self.assertIn("artworks", rep_todo["materials"])
        # 未轮到的责任方没有待办
        self.assertEqual([], ctx.tour.list_todos("ins1"))
        ctx.tour.confirm(request_id="cf", actor_id="rep1", plan_id="tour-001", version=1)
        ins_todo = ctx.tour.list_todos("ins1")[0]
        self.assertEqual("ins-pol-1", ins_todo["materials"]["policy_id"])
        self.assertEqual(["art1"], [a["artwork_id"] for a in ins_todo["materials"]["artworks"]])
        ctx.tour.confirm(request_id="cf2", actor_id="ins1", plan_id="tour-001", version=1)
        venue_todo = ctx.tour.list_todos("venue1")[0]
        # 同一场馆负责两个停点：只有一个待办，但资料中包含两个停点
        self.assertEqual(2, len(venue_todo["materials"]["stops"]))
        ctx.tour.confirm(request_id="cf3", actor_id="venue1", plan_id="tour-001", version=1)
        carrier_todo = ctx.tour.list_todos("carrier1")[0]
        self.assertEqual(2, len(carrier_todo["materials"]["segments"]))
        # 另一承运方看不到任何区段
        self.assertEqual([], [t for t in ctx.tour.list_todos("carrier2")])

    def test_receiver_gets_handover_todo_after_dispatch(self):
        ctx = self.ctx
        ctx.draft_and_publish()
        ctx.tour.dispatch_segment(request_id="dp", actor_id="carrier1",
                                  plan_id="tour-001", segment_key="seg1")
        todos = ctx.tour.list_todos("recv1")
        self.assertTrue(any(t["kind"] == "handover" and t["segment_key"] == "seg1" for t in todos))
        # 非接收人看不到
        self.assertFalse(any(t["kind"] == "handover" for t in ctx.tour.list_todos("recv2")))

    def test_handover_requires_qualified_receiver(self):
        ctx = self.ctx
        ctx.draft_and_publish()
        ctx.tour.dispatch_segment(request_id="dp", actor_id="carrier1",
                                  plan_id="tour-001", segment_key="seg1")
        with self.assertRaises(PermissionDenied):
            ctx.tour.complete_handover(request_id="hd", actor_id="recv2",
                                       plan_id="tour-001", segment_key="seg1",
                                       condition_note="尝试接收")


class PersistenceTest(unittest.TestCase):
    def test_state_survives_restart_including_leases_and_in_transit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tour.sqlite3"
            ctx = TourFixture(path)
            ctx.draft_and_publish()
            ctx.tour.dispatch_segment(request_id="dp", actor_id="carrier1",
                                      plan_id="tour-001", segment_key="seg1")
            ctx.database.close()

            restarted = Database(path)
            tour = TourService(restarted, FixedClock(T0 + timedelta(hours=1)))
            plan = tour.get_plan("tour-001")
            self.assertEqual(1, plan["effective_version"])
            seg1 = [s for s in plan["segment_states"] if s["segment_key"] == "seg1"][0]
            self.assertEqual("in_transit", seg1["state"])
            # 待确认顺序仍可查询（无草案时为空；有在途交接待办）
            self.assertTrue(any(t["kind"] == "handover" for t in tour.list_todos("recv1")))
            # confirmed 租约仍然占用
            active = restarted.connection.execute(
                "SELECT COUNT(*) c FROM resource_allocations WHERE plan_id='tour-001' "
                "AND status='confirmed'").fetchone()["c"]
            self.assertGreater(active, 0)
            restarted.close()


class RestoreTest(unittest.TestCase):
    def test_restore_at_reconstructs_chain(self):
        ctx = TourFixture()
        ctx.tour.create_draft(request_id="d1", actor_id="op1", manifest=ctx.manifest())
        ctx.confirm_all()
        ctx.publish_plan()
        at_day_5 = ctx.tour.restore_at("tour-001", ts(5))
        self.assertEqual(1, at_day_5["effective_version"])
        actors = {c["decided_by"] for c in at_day_5["confirmations"]}
        self.assertEqual({"rep1", "ins1", "venue1", "carrier1"}, actors)
        before = ctx.tour.restore_at("tour-001", ts(-1))
        self.assertIsNone(before["effective_version"])
        valid, _ = ctx.svc.verify_audit()
        self.assertTrue(valid)
        ctx.close()


if __name__ == "__main__":
    unittest.main()
