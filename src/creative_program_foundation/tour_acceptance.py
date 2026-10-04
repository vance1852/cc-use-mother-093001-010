"""巡展与渠道调度服务的离线端到端验收。

覆盖：渠道建档 → 限时草案占用 → 按顺序确认 → 发布互斥 → 发运/合规交接
→ 场馆临时闭馆按稳定优先级启用备用场馆 → 已交接与费用不被覆盖
→ 服务重启后租约与在途状态一致 → 审计时点还原责任链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .service import DomainService
from .storage import Database
from .tour import TourService

T0 = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)


def _ts(day: int, hour: int = 0) -> str:
    return (T0 + timedelta(days=day, hours=hour)).isoformat().replace("+00:00", "Z")


def _bootstrap(svc: DomainService, tour: TourService) -> None:
    svc.register_organization(request_id="org", actor_id="bootstrap",
                               organization_id="o1", name="巡展主办机构")
    actors = [
        ("a1", "管理员", "admin"), ("op1", "运营", "operator"),
        ("rep1", "作品代表", "reviewer"), ("ins1", "保险经办", "insurance"),
        ("venue1", "商场联系人", "venue"), ("venue2", "博物馆联系人", "venue"),
        ("recv1", "接收人", "receiver"), ("carrier1", "承运方", "carrier")]
    for i, (actor_id, name, role) in enumerate(actors):
        svc.register_actor(request_id=f"actor-{actor_id}", actor_id="bootstrap" if i == 0 else "a1",
                           new_actor_id=actor_id, display_name=name,
                           role=role, organization_id="o1")
    svc.register_site(request_id="site", actor_id="op1", site_id="s1",
                      organization_id="o1", name="城市节点", timezone_name="Asia/Shanghai")
    tour.register_artwork(request_id="aw1", actor_id="op1", artwork_id="art1",
                          title="金奖装置", representative_id="rep1", declared_value=600000,
                          spec={"pieces": 3, "weight_kg": 120, "volume_m3": 4,
                                "install_hours": 8, "climate": True, "security": True})
    for vid, contact, name in (("venueMall", "venue1", "商场中庭"),
                               ("venueMuseum", "venue2", "城市博物馆")):
        tour.register_venue(request_id=f"v-{vid}", actor_id="op1", venue_id=vid, site_id="s1",
                            name=name, contact_actor_id=contact,
                            required_qualification="fine_art_level2")
    for suffix, vid in (("mall", "venueMall"), ("museum", "venueMuseum")):
        tour.register_venue_window(request_id=f"w-{suffix}", actor_id="op1",
                                   window_id=f"win-{suffix}", venue_id=vid,
                                   starts_at=_ts(10), ends_at=_ts(20))
        tour.register_display_case(request_id=f"c-{suffix}", actor_id="op1",
                                   case_id=f"case-{suffix}", venue_id=vid, label="恒温展柜",
                                   conditions={"max_weight_kg": 800, "max_volume_m3": 30,
                                               "climate": True, "security": True})
        tour.register_labor(request_id=f"l-{suffix}", actor_id="op1",
                            labor_id=f"labor-{suffix}", venue_id=vid,
                            starts_at=_ts(10), ends_at=_ts(20), available_hours=60)
        tour.register_receiver(request_id=f"r-{suffix}", actor_id="op1",
                               receiver_id=f"recv-{suffix}", venue_id=vid,
                               receiver_actor_id="recv1",
                               qualification="fine_art_level2")
    tour.register_carrier(request_id="cr1", actor_id="op1", carrier_id="carA",
                          name="艺术品运输甲", contact_actor_id="carrier1")
    tour.register_carrier_slot(request_id="cs1", actor_id="op1", slot_id="slot-a1",
                               carrier_id="carA", starts_at=_ts(7), ends_at=_ts(11),
                               capacity={"max_weight_kg": 800, "max_volume_m3": 30,
                                         "max_pieces": 20})
    tour.register_insurance_policy(request_id="pol1", actor_id="op1", policy_id="pol-a1",
                                   name="全程艺术品险", handler_actor_id="ins1",
                                   limit_amount=3000000, starts_at=_ts(5), ends_at=_ts(25))


def _manifest() -> dict:
    return {
        "plan_id": "tour-gold-2026",
        "artwork_ids": ["art1"],
        "rep_actor_id": "rep1",
        "lease_seconds": 900,
        "stops": [{
            "stop_key": "city-show",
            "options": [
                {"venue_id": "venueMall", "window_id": "win-mall", "case_id": "case-mall",
                 "labor_id": "labor-mall", "receiver_id": "recv-mall",
                 "install_at": _ts(11, 1), "open_at": _ts(11, 4),
                 "close_at": _ts(18, 20), "dismantle_at": _ts(19, 2), "cost": 5000},
                {"venue_id": "venueMuseum", "window_id": "win-museum", "case_id": "case-museum",
                 "labor_id": "labor-museum", "receiver_id": "recv-museum",
                 "install_at": _ts(11, 1), "open_at": _ts(11, 4),
                 "close_at": _ts(18, 20), "dismantle_at": _ts(19, 2), "cost": 6500}],
            "sessions": [{"session_key": "open-day", "title": "开幕公众导览",
                          "starts_at": _ts(11, 6), "ends_at": _ts(11, 9), "announce": True}],
            "publicity": {"commitment": "开幕前 48 小时发布媒体排期", "deadline": _ts(9)}}],
        "segments": [{
            "segment_key": "inbound", "from_stop_key": None, "to_stop_key": "city-show",
            "options": [
                {"carrier_id": "carA", "slot_id": "slot-a1",
                 "pickup_at": _ts(8), "deliver_at": _ts(10, 12), "cost": 2000}]}],
        "insurance": [{"policy_id": "pol-a1", "artwork_ids": ["art1"],
                       "cover_from": _ts(7), "cover_to": _ts(20), "cost": 1200}]}


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tour_acceptance.sqlite3"
        clock = FixedClock(T0)
        database = Database(path)
        svc = DomainService(database, clock)
        tour = TourService(database, clock)
        _bootstrap(svc, tour)

        draft = tour.create_draft(request_id="draft-1", actor_id="op1", manifest=_manifest())
        assert draft["status"] == "draft"
        for i, actor_id in enumerate(("rep1", "ins1", "venue1", "carrier1")):
            tour.confirm(request_id=f"cf-{i}", actor_id=actor_id,
                         plan_id="tour-gold-2026", version=1)
        published = tour.publish(request_id="pub-1", actor_id="op1",
                                 plan_id="tour-gold-2026", version=1)
        assert published["status"] == "published"

        tour.dispatch_segment(request_id="dp-1", actor_id="carrier1",
                              plan_id="tour-gold-2026", segment_key="inbound")
        handover = tour.complete_handover(request_id="hd-1", actor_id="recv1",
                                          plan_id="tour-gold-2026", segment_key="inbound",
                                          condition_note="3 件组件齐全，外包装完好")
        assert handover["state"] == "delivered"

        # 商场临时闭馆：按 options 稳定优先级切到博物馆
        clock._value = T0 + timedelta(days=12)
        closed = tour.close_venue(request_id="close-mall", actor_id="venue1",
                                  venue_id="venueMall", reason="商场临时消防演练",
                                  notice_confirmed=True)
        reroute = closed["reroutes"][0]
        assert reroute["status"] == "draft" and reroute["version"] == 2
        chosen = {a["stop_key"]: a["option_index"] for a in reroute["attempts"] if not a["blockers"]}
        assert chosen == {"city-show": 1}
        # 重排只影响停点，已交接 inbound 区段不重排
        assert reroute["changed_segments"] == []
        tour.confirm(request_id="cf2-rep", actor_id="rep1", plan_id="tour-gold-2026", version=2)
        tour.confirm(request_id="cf2-venue", actor_id="venue2", plan_id="tour-gold-2026", version=2)
        tour.publish(request_id="pub-2", actor_id="op1", plan_id="tour-gold-2026", version=2)

        costs_before_restart = tour.get_costs("tour-gold-2026")
        assert costs_before_restart["total"] == 5000 + 6500 + 2000 + 1200

        # 重启：租约、在途状态、待确认顺序必须一致
        database.close()
        database = Database(path)
        tour2 = TourService(database, FixedClock(T0 + timedelta(days=12, hours=1)))
        reconciled = tour2.reconcile()
        assert reconciled == 0
        plan = tour2.get_plan("tour-gold-2026")
        assert plan["effective_version"] == 2
        inbound = [s for s in plan["segment_states"] if s["segment_key"] == "inbound"][0]
        assert inbound["state"] == "delivered"

        # 审计时点还原
        at_v1 = tour2.restore_at("tour-gold-2026", _ts(11))
        at_v2 = tour2.restore_at("tour-gold-2026", _ts(15))
        assert at_v1["effective_version"] == 1
        assert at_v2["effective_version"] == 2
        assert len(at_v2["handovers"]) == 1
        from .audit import verify_chain
        chain_valid, chain_count = verify_chain(database.connection)
        database.close()
        return {"status": "ok", "effective_version": 2,
                "costs_total": costs_before_restart["total"],
                "handovers": 1, "reconciled_expired": reconciled,
                "restore_v1": at_v1["effective_version"],
                "restore_v2": at_v2["effective_version"],
                "audit_valid": chain_valid, "audit_events": chain_count}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
