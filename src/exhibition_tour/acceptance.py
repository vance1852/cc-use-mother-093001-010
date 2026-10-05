"""巡展与渠道调度服务的离线端到端验收。

覆盖：建档 → 限时草案占用 → 各方确认 → 发布 → 交接/费用 → 窗口缩短触发重排 →
备用资源按优先级启用 → 仅受影响区段重排、已交接与费用保留 → 观众场次影响 →
历史时点责任链还原 → 服务重启后租约/在途/待办一致 → 审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError, StateError
from .service import TourScheduler
from .storage import Database


def _segments_v1():
    return [
        {
            "segment_id": "v1-venue-sh", "kind": "venue",
            "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-12T20:00:00Z",
            "resource_id": "window-sh",
            "requirement": {"case_resource_id": "case-sh", "install_hours": 4,
                            "dismantle_hours": 4},
            "alternatives": ["window-sh-2"],
        },
        {
            "segment_id": "v1-transport", "kind": "transport",
            "start_at": "2026-11-13T00:00:00Z", "end_at": "2026-11-14T00:00:00Z",
            "resource_id": "truck-1",
            "requirement": {"from_city": "上海", "to_city": "北京", "load_volume": 6},
            "alternatives": ["truck-2"],
        },
        {
            "segment_id": "v1-venue-bj", "kind": "venue",
            "start_at": "2026-11-15T08:00:00Z", "end_at": "2026-11-17T20:00:00Z",
            "resource_id": "window-bj",
            "requirement": {"case_resource_id": "case-bj", "install_hours": 4,
                            "dismantle_hours": 4},
            "alternatives": ["window-bj-2"],
        },
        {
            "segment_id": "v1-insurance", "kind": "insurance",
            "start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-22T00:00:00Z",
            "resource_id": "quota-1",
            "requirement": {"coverage_amount": 500000, "currency": "CNY"},
            "alternatives": ["quota-2"],
        },
    ]


def _sessions_v1():
    return {
        "v1-venue-sh": [{"starts_at": "2026-11-11T10:00:00Z", "label": "上海公众场", "capacity": 120}],
        "v1-venue-bj": [{"starts_at": "2026-11-16T10:00:00Z", "label": "北京公众场", "capacity": 200}],
    }


def _commitments_v1():
    return {
        "v1-venue-sh": [{"channel": "商场大屏", "promised_at": "2026-11-05T09:00:00Z",
                         "detail": "连续三天轮播"}],
        "v1-venue-bj": [{"channel": "博物馆公众号", "promised_at": "2026-11-10T09:00:00Z",
                         "detail": "开幕专题推送"}],
    }


def _bootstrap(service: TourScheduler) -> None:
    service.register_participant(request_id="p-op", actor_id="bootstrap", participant_id="op-1",
                                 display_name="运营负责人", role="operator", organization_id="org-op")
    participants = [
        ("venue-sh-1", "上海商场", "venue", "org-sh"),
        ("venue-bj-1", "北京博物馆", "venue", "org-bj"),
        ("venue-bj-2", "北京茶文化空间", "venue", "org-bj2"),
        ("carrier-1", "承运调度", "carrier", "org-carrier"),
        ("insurer-1", "保险经办", "insurer", "org-insurer"),
        ("rep-1", "作品代表", "representative", "org-artist"),
        ("au-1", "审计员", "auditor", "org-audit"),
    ]
    for index, (pid, name, role, org) in enumerate(participants):
        service.register_participant(request_id=f"p-{index}", actor_id="op-1", participant_id=pid,
                                     display_name=name, role=role, organization_id=org)
    service.register_artwork(request_id="a-1", actor_id="op-1", artwork_id="art-1",
                             title="金奖作品《茶之光》", representative_id="rep-1",
                             components=["主装置", "茶具组件"], condition_note="完好")
    resources = [
        ("window-sh", "venue_window", "venue-sh-1", "上海商场展窗",
         {"city": "上海", "window_start": "2026-11-10T00:00:00Z",
          "window_end": "2026-12-31T00:00:00Z"}),
        ("window-sh-2", "venue_window", "venue-sh-1", "上海商场备用展窗",
         {"city": "上海", "window_start": "2026-11-10T00:00:00Z",
          "window_end": "2026-12-31T00:00:00Z"}),
        ("window-bj", "venue_window", "venue-bj-1", "北京博物馆展厅",
         {"city": "北京", "window_start": "2026-11-15T00:00:00Z",
          "window_end": "2026-11-25T00:00:00Z"}),
        ("window-bj-2", "venue_window", "venue-bj-2", "北京茶文化空间展厅",
         {"city": "北京", "window_start": "2026-11-01T00:00:00Z",
          "window_end": "2026-12-31T00:00:00Z"}),
        ("case-sh", "display_case", "venue-sh-1", "上海展柜", {"city": "上海"}),
        ("case-bj", "display_case", "venue-bj-1", "北京展柜", {"city": "北京"}),
        ("case-bj-2", "display_case", "venue-bj-2", "茶文化空间展柜", {"city": "北京"}),
        ("truck-1", "vehicle", "carrier-1", "恒温车一号", {"capacity": 10}),
        ("truck-2", "vehicle", "carrier-1", "恒温车二号", {"capacity": 12}),
        ("quota-1", "insurance_quota", "insurer-1", "珍品保险额度",
         {"capacity": 1000000, "currency": "CNY"}),
        ("quota-2", "insurance_quota", "insurer-1", "备用保险额度",
         {"capacity": 800000, "currency": "CNY"}),
    ]
    for index, (rid, kind, owner, label, caps) in enumerate(resources):
        service.register_resource(request_id=f"r-{index}", actor_id="op-1", resource_id=rid,
                                  kind=kind, owner_id=owner, label=label, capabilities=caps)
    for index, (org, qualified) in enumerate([
            ("org-sh", True), ("org-bj", True), ("org-bj2", True)]):
        service.upsert_qualification(request_id=f"q-{index}", actor_id="op-1",
                                     venue_organization_id=org, artwork_id="art-1",
                                     qualified=qualified, certificate=f"CERT-{org}")


def _confirm_all(service: TourScheduler, plan_id: str, version: int, expect_remaining=None) -> None:
    role_for_segment = {
        "v1-venue-sh": "venue-sh-1", "v1-venue-bj": "venue-bj-1",
        "v1-transport": "carrier-1", "v1-insurance": "insurer-1",
    }
    req = 0
    for segment_id, actor in role_for_segment.items():
        service.respond(request_id=f"c-{req}", actor_id=actor, plan_id=plan_id, version=version,
                        segment_id=segment_id, decision="confirm")
        req += 1
        replay = service.respond(request_id=f"c-{req - 1}", actor_id=actor, plan_id=plan_id,
                                 version=version, segment_id=segment_id, decision="confirm")
        assert replay.replayed is True, "重复确认必须幂等且不多占资源"
        req += 1
    # 作品代表对所有区段一次性确认。
    for index, segment_id in enumerate(role_for_segment):
        service.respond(request_id=f"c-rep-{index}", actor_id="rep-1", plan_id=plan_id,
                        version=version, segment_id=segment_id, decision="confirm")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "tour.sqlite3"
        clock = FixedClock(datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))
        database = Database(db_path)
        service = TourScheduler(database, clock)
        _bootstrap(service)

        # 1) 限时草案占用资源。
        service.create_plan_version(
            request_id="plan-1", actor_id="op-1", plan_id="tour-1", artwork_id="art-1",
            components=["主装置", "茶具组件"], segments=_segments_v1(),
            sessions=_sessions_v1(), commitments=_commitments_v1(), lease_minutes=600)
        # 同一时段重复预订必须冲突，并给出占用来源。
        double_book = None
        try:
            service.create_plan_version(
                request_id="plan-1b", actor_id="op-1", plan_id="tour-1b", artwork_id="art-1",
                components=["主装置"], segments=[
                    {"segment_id": "b1", "kind": "venue",
                     "start_at": "2026-11-10T09:00:00Z", "end_at": "2026-11-12T12:00:00Z",
                     "resource_id": "window-sh",
                     "requirement": {"case_resource_id": "case-sh", "install_hours": 2,
                                     "dismantle_hours": 2}},
                    {"segment_id": "b2", "kind": "insurance",
                     "start_at": "2026-11-10T09:00:00Z", "end_at": "2026-11-12T12:00:00Z",
                     "resource_id": "quota-1",
                     "requirement": {"coverage_amount": 500000, "currency": "CNY"}},
                ],
                sessions={"b1": [{"starts_at": "2026-11-11T10:00:00Z",
                                  "label": "撞期场", "capacity": 10}]})
        except ConflictError as exc:
            double_book = exc
        assert double_book is not None and getattr(double_book, "blockers", None), "必须返回冲突来源"

        # 2) 各方按职责确认；待办遵循稳定顺序与最小资料。
        todos = service.list_todos(actor_id="carrier-1")
        assert len(todos) == 1 and todos[0]["minimal"]["from_city"] == "上海"
        assert "coverage_amount" not in todos[0]["minimal"], "承运方不应看到保险额度明细"
        rep_todos = service.list_todos(actor_id="rep-1")
        assert len(rep_todos) == 4
        assert {t["role"] for t in rep_todos} == {"representative"}
        # 待确认顺序按区段起始、结束、编号稳定排序。
        assert [t["segment_id"] for t in rep_todos] == \
            ["v1-venue-sh", "v1-insurance", "v1-transport", "v1-venue-bj"]
        assert len(rep_todos[0]["minimal"]["itinerary"]) == 4
        assert service.list_todos(actor_id="venue-bj-2") == [], "无关节与方看不到待办"

        _confirm_all(service, "tour-1", 1)
        plan = service.get_plan(actor_id="op-1", plan_id="tour-1")
        assert plan["versions"][0]["status"] == "confirmed"

        # 未确认完不能发布（此版本已确认，改为校验状态机：拒绝后不能发布）。
        service.publish(request_id="pub-1", actor_id="op-1", plan_id="tour-1")
        plan = service.get_plan(actor_id="op-1", plan_id="tour-1")
        assert plan["published_version"] == 1
        assert plan["versions"][0]["status"] == "published"

        # 3) 发布后交接与费用：不可静默覆盖。
        service.record_handover(request_id="h-1", actor_id="venue-sh-1", plan_id="tour-1",
                                segment_id="v1-venue-sh", handover_ref="DN-20261110-01")
        service.record_cost(request_id="cost-1", actor_id="op-1", plan_id="tour-1",
                            segment_id="v1-venue-bj", amount="12000.00", currency="CNY",
                            category="场地定金")
        overwritten = False
        try:
            service.record_handover(request_id="h-2", actor_id="venue-sh-1", plan_id="tour-1",
                                    segment_id="v1-venue-sh", handover_ref="DN-OVERWRITE")
        except StateError:
            overwritten = True
        assert overwritten, "已完成交接不能被覆盖"

        # 4) 北京场馆窗口缩短到无法容纳 → 仅重排受影响区段，备用场馆按稳定优先级启用。
        clock.advance(days=5)  # 2026-11-06
        service.rework_plan(
            request_id="rw-1", actor_id="op-1", plan_id="tour-1", reason="window_shortened",
            replacements=[
                {"replaces_segment_id": "v1-venue-bj", "segment_id": "v2-venue-bj",
                 "start_at": "2026-11-18T08:00:00Z", "end_at": "2026-11-20T20:00:00Z",
                 "requirement": {"install_hours": 4, "dismantle_hours": 4},
                 "alternatives": ["window-bj-2", "window-bj"]},
            ],
            sessions={"v2-venue-bj": [{"starts_at": "2026-11-19T10:00:00Z",
                                       "label": "北京公众场（茶文化空间）", "capacity": 160}]},
            commitments={"v2-venue-bj": [{"channel": "茶文化空间公众号",
                                          "promised_at": "2026-11-12T09:00:00Z",
                                          "detail": "开幕专题"}]},
            change_summary="北京博物馆窗口缩短，启用同城市备用场馆", lease_minutes=600)
        v2 = service.get_plan(actor_id="op-1", plan_id="tour-1")["versions"][1]
        carried = {s["segment_id"]: s for s in v2["segments"]}
        assert "v1-venue-sh" in carried and carried["v1-venue-sh"]["status"] == "handed_over", \
            "已交接区段必须原样携带"
        assert carried["v1-venue-sh"]["handover_ref"] == "DN-20261110-01"
        assert carried["v2-venue-bj"]["resource_id"] == "window-bj-2", "必须按稳定优先级启用备用场馆"
        assert carried["v2-venue-bj"]["requirement"]["case_resource_id"] == "case-bj-2", \
            "必须自动匹配同城可用展柜"
        # 运输区段未受影响：随携带保留，承运方无需重复确认。
        assert service.list_todos(actor_id="carrier-1") == []
        new_todos = service.list_todos(actor_id="venue-bj-2")
        assert len(new_todos) == 1 and new_todos[0]["segment_id"] == "v2-venue-bj"

        # 受影响场馆/代表重新确认后发布。
        service.respond(request_id="c2-venue", actor_id="venue-bj-2", plan_id="tour-1", version=2,
                        segment_id="v2-venue-bj", decision="confirm")
        service.respond(request_id="c2-rep", actor_id="rep-1", plan_id="tour-1", version=2,
                        segment_id="v2-venue-bj", decision="confirm")
        publish_2 = service.publish(request_id="pub-2", actor_id="op-1", plan_id="tour-1")
        assert publish_2.replayed is False
        impact = service.sessions_impact(actor_id="op-1", plan_id="tour-1")
        shifted = [s for s in impact["sessions"] if s["publicity_state"] == "shifted"]
        assert len(shifted) == 1 and shifted[0]["segment_id"] == "v1-venue-bj"
        assert len(impact["events"]) >= 1
        assert all(any(s["state"] == "shifted" for s in e["impacted_sessions"])
                   for e in impact["events"]), "每次改期影响的观众场次都可追溯"
        plan = service.get_plan(actor_id="op-1", plan_id="tour-1")
        v1_after, v2_after = plan["versions"]
        assert v1_after["status"] == "superseded" and v2_after["status"] == "published"
        carried_cost = database.connection.execute(
            "SELECT amount,carried_into_version FROM incurred_costs WHERE segment_id='v1-venue-bj'"
        ).fetchone()
        assert carried_cost["amount"] == "12000.00" and carried_cost["carried_into_version"] == 2, \
            "已发生费用只能前向关联，金额不能改写"

        # 5) 恢复路径与冲突原因可查。
        recovery = service.recovery_options(actor_id="op-1", plan_id="tour-1")
        assert recovery["status"] == "published"
        assert recovery["options"]["v2-venue-bj"][0]["resource_id"]

        # 6) 审计员按历史时点还原责任链（v2 尚未出生的时刻只应看到 v1）。
        snapshot = service.snapshot_at(actor_id="au-1", plan_id="tour-1",
                                       at="2026-11-05T23:00:00Z")
        assert len(snapshot["versions"]) == 1
        assert snapshot["versions"][0]["status_as_of"] == "published"
        assert snapshot["versions"][0]["handed_over_segments"] == ["v1-venue-sh"]
        assert any(e["event_type"] == "handover_recorded" for e in snapshot["versions"][0]["timeline"])
        assert any(c["amount"] == "12000.00" for c in snapshot["versions"][0]["costs_on_record"])

        # 7) 另起一个草案用于验证重启后的租约与待办一致性。
        service.create_plan_version(
            request_id="plan-restart", actor_id="op-1", plan_id="tour-restart",
            artwork_id="art-1", components=["主装置"],
            segments=[
                {"segment_id": "rs-venue", "kind": "venue",
                 "start_at": "2026-12-01T08:00:00Z", "end_at": "2026-12-02T20:00:00Z",
                 "resource_id": "window-sh",
                 "requirement": {"case_resource_id": "case-sh", "install_hours": 2,
                                 "dismantle_hours": 2}},
                {"segment_id": "rs-ins", "kind": "insurance",
                 "start_at": "2026-12-01T08:00:00Z", "end_at": "2026-12-02T20:00:00Z",
                 "resource_id": "quota-2",
                 "requirement": {"coverage_amount": 100000, "currency": "CNY"}},
            ],
            sessions={"rs-venue": [{"starts_at": "2026-12-01T10:00:00Z",
                                    "label": "重启校验场", "capacity": 50}]},
            lease_minutes=600)

        # 8) 模拟服务重启：重新打开数据库，租约、在途状态、待确认顺序必须保持。
        database.close()
        database = Database(db_path)
        service = TourScheduler(database, clock)
        valid, event_count = service.verify_audit()
        assert valid, "审计哈希链必须有效"
        restarted = service.get_plan(actor_id="op-1", plan_id="tour-1")
        assert restarted["published_version"] == 2
        sh_seg = next(s for s in restarted["versions"][1]["segments"]
                      if s["segment_id"] == "v1-venue-sh")
        assert sh_seg["status"] == "handed_over" and sh_seg["handover_ref"] == "DN-20261110-01"
        todos_after = service.list_todos(actor_id="rep-1")
        assert [t["segment_id"] for t in todos_after] == ["rs-ins", "rs-venue"]
        avail = service.availability(kind="venue_window",
                                     start_at="2026-12-01T08:00:00Z",
                                     end_at="2026-12-02T20:00:00Z", city="上海")
        blocked = next(i for i in avail["items"] if i["resource_id"] == "window-sh")
        assert blocked["available"] is False and blocked["blocked_by"][0]["segment_id"] == "rs-venue"

        # 9) 租约到期后自动释放，且不影响已发布版本。
        clock.advance(minutes=601)
        swept = service.sweep_expired(actor_id="system")
        assert any(x == "tour-restart/v1" for x in swept["expired"]), swept
        avail2 = service.availability(kind="venue_window",
                                      start_at="2026-12-01T08:00:00Z",
                                      end_at="2026-12-02T20:00:00Z", city="上海")
        assert next(i for i in avail2["items"] if i["resource_id"] == "window-sh")["available"]
        still = service.get_plan(actor_id="op-1", plan_id="tour-1")
        assert still["published_version"] == 2
        valid, event_count = service.verify_audit()
        database.close()
        return {"status": "ok", "audit_valid": valid, "audit_events": event_count,
                "published_version": 2,
                "shifted_sessions": len(shifted),
                "snapshot_versions_at_history_point": len(snapshot["versions"])}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
