"""巡展与渠道调度领域服务。

把作品及组件、场馆窗口、展柜条件、布撤展工时、运输区段、承运能力、保险额度、
接收资质与宣传承诺编入同一计划版本：

- 草案以限时租约占用资源，场馆/承运/保险/作品代表按职责确认后才可发布；
- 拒绝、窗口缩短、运输延误、作品损伤、临时闭馆只能重排受影响区段；
- 已完成交接、已发生费用、公开场次只能追加留痕，不能被静默覆盖；
- 备用资源按稳定优先级启用；并发发布最多一个版本生效。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest, verify_chain
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, StateError, ValidationError
from .models import (
    ALL_ROLES,
    PLAN_CONFIRMED,
    PLAN_DRAFT,
    PLAN_EXPIRED,
    PLAN_PUBLISHED,
    PLAN_REJECTED,
    PLAN_SUPERSEDED,
    REWORK_REASONS,
    ROLE_AUDITOR,
    ROLE_CARRIER,
    ROLE_INSURER,
    ROLE_OPERATOR,
    ROLE_REPRESENTATIVE,
    ROLE_VENUE,
    SEG_HANDED_OVER,
    SEG_INSURANCE,
    SEG_PENDING,
    SEG_TRANSPORT,
    SEG_VENUE,
    SEGMENT_KINDS,
    Participant,
    WriteReceipt,
)
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RESOURCE_KIND_FOR_SEGMENT = {
    SEG_VENUE: "venue_window",
    SEG_TRANSPORT: "vehicle",
    SEG_INSURANCE: "insurance_quota",
}
RESOURCE_OWNER_ROLE = {
    "venue_window": ROLE_VENUE,
    "display_case": ROLE_VENUE,
    "vehicle": ROLE_CARRIER,
    "insurance_quota": ROLE_INSURER,
}
CONFIRM_ROLES_FOR_SEGMENT = {
    SEG_VENUE: (ROLE_VENUE, ROLE_REPRESENTATIVE),
    SEG_TRANSPORT: (ROLE_CARRIER, ROLE_REPRESENTATIVE),
    SEG_INSURANCE: (ROLE_INSURER, ROLE_REPRESENTATIVE),
}
ROLE_ORDER = {ROLE_VENUE: 0, ROLE_CARRIER: 1, ROLE_INSURER: 2, ROLE_REPRESENTATIVE: 3}
DEFAULT_LEASE_MINUTES = 30
MAX_LEASE_MINUTES = 1440


class TourScheduler:
    """协调租约、确认、发布、重排、审计与查询规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now(self) -> str:
        return self._ts(self._now_dt())

    @staticmethod
    def _ts(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _dt(value: Any, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须带时区")
        return parsed.astimezone(timezone.utc)

    def _id(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    @staticmethod
    def _note(value: Any, limit: int = 300) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _loads(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _jdump(value: Any) -> str:
        return canonical_json(value)

    def _participant(self, conn, participant_id: str) -> Participant:
        row = conn.execute("SELECT * FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
        if row is None:
            raise NotFoundError("参与方不存在")
        participant = Participant(row["participant_id"], row["display_name"], row["role"],
                                  row["organization_id"], bool(row["active"]))
        if not participant.active:
            raise PermissionDenied("参与方已停用")
        return participant

    @staticmethod
    def _require(participant: Participant, *roles: str) -> None:
        if participant.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _replay(self, conn, *, request_id: str, action: str,
                payload: dict[str, Any]) -> WriteReceipt | None:
        """幂等回执命中时原样返回；内容不一致则冲突。"""

        request_id = self._id(request_id, "request_id")
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _store_receipt(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                       resource_type: str, resource_id: str, response: dict[str, Any]) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _timeline(self, conn, plan_id: str, version: int, event_type: str, actor_id: str,
                  detail: dict[str, Any], at: str | None = None) -> None:
        at = at or self._now()
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),-1)+1 AS next FROM version_timeline "
            "WHERE plan_id=? AND version=?", (plan_id, version),
        ).fetchone()["next"]
        conn.execute(
            "INSERT INTO version_timeline(plan_id,version,sequence,event_type,actor_id,at,detail_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (plan_id, version, sequence, event_type, actor_id, at, self._jdump(detail)),
        )

    def _audit(self, conn, *, actor_id: str, action: str, resource_type: str, resource_id: str,
               detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------------
    # 参与方 / 作品 / 资源 / 资质登记
    # ------------------------------------------------------------------
    def register_participant(self, *, request_id: str, actor_id: str, participant_id: str,
                             display_name: str, role: str, organization_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id, "display_name": display_name,
                   "role": role, "organization_id": organization_id}
        with self.database.transaction(immediate=True) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM participants").fetchone()["c"]
            if count:
                actor = self._participant(conn, actor_id)
                self._require(actor, ROLE_OPERATOR)
            elif actor_id != "bootstrap":
                raise PermissionDenied("首位参与方必须由 bootstrap 建档")
            participant_id = self._id(participant_id, "participant_id")
            display_name = self._text(display_name, "display_name")
            organization_id = self._id(organization_id, "organization_id")
            if role not in ALL_ROLES:
                raise ValidationError("role 不在允许范围内")
            replay = self._replay(conn, request_id=request_id, action="register_participant",
                                  payload=payload)
            if replay:
                return replay

            try:
                conn.execute(
                    "INSERT INTO participants(participant_id,display_name,role,organization_id,active,created_at) "
                    "VALUES(?,?,?,?,1,?)",
                    (participant_id, display_name, role, organization_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("参与方编号已经存在") from exc
            self._audit(conn, actor_id=actor_id, action="participant.registered",
                        resource_type="participant", resource_id=participant_id,
                        detail={"display_name": display_name, "role": role,
                                "organization_id": organization_id})
            return self._store_receipt(conn, request_id=request_id, action="register_participant",
                                      payload=payload, resource_type="participant",
                                      resource_id=participant_id,
                                      response={"participant_id": participant_id})

    def register_artwork(self, *, request_id: str, actor_id: str, artwork_id: str, title: str,
                         representative_id: str, components: list[str],
                         condition_note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "artwork_id": artwork_id, "title": title,
                   "representative_id": representative_id, "components": components}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR)
            rep = self._participant(conn, representative_id)
            if rep.role != ROLE_REPRESENTATIVE:
                raise ValidationError("representative_id 必须是作品代表")
            artwork_id = self._id(artwork_id, "artwork_id")
            title = self._text(title, "title")
            if not isinstance(components, list) or not components:
                raise ValidationError("components 必须是非空列表")
            components = [self._text(c, "component", 120) for c in components]
            if len(set(components)) != len(components):
                raise ValidationError("组件不能重复")
            replay = self._replay(conn, request_id=request_id, action="register_artwork", payload=payload)
            if replay:
                return replay

            try:
                conn.execute(
                    "INSERT INTO artworks(artwork_id,title,representative_id,components_json,"
                    "condition_note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (artwork_id, title, representative_id, self._jdump(components),
                     str(condition_note)[:500], actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("作品编号已经存在") from exc
            self._audit(conn, actor_id=actor_id, action="artwork.registered",
                        resource_type="artwork", resource_id=artwork_id,
                        detail={"title": title, "representative_id": representative_id,
                                "components": components})
            return self._store_receipt(conn, request_id=request_id, action="register_artwork",
                                      payload=payload, resource_type="artwork",
                                      resource_id=artwork_id, response={"artwork_id": artwork_id})

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str, kind: str,
                          owner_id: str, label: str,
                          capabilities: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "resource_id": resource_id, "kind": kind,
                   "owner_id": owner_id, "capabilities": capabilities or {}}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            owner = self._participant(conn, owner_id)
            if kind not in RESOURCE_OWNER_ROLE:
                raise ValidationError("资源种类不支持")
            if actor.role != ROLE_OPERATOR and actor.participant_id != owner_id:
                raise PermissionDenied("只有运营人员或资源所有方可以登记资源")
            if owner.role != RESOURCE_OWNER_ROLE[kind]:
                raise ValidationError("资源所有方角色与资源种类不匹配")
            self._validate_capabilities(kind, capabilities or {})
            resource_id = self._id(resource_id, "resource_id")
            label = self._text(label, "label")
            replay = self._replay(conn, request_id=request_id, action="register_resource", payload=payload)
            if replay:
                return replay

            try:
                conn.execute(
                    "INSERT INTO resources(resource_id,kind,owner_id,label,capabilities_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (resource_id, kind, owner_id, label,
                     self._jdump(capabilities or {}), self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("资源编号已经存在") from exc
            self._audit(conn, actor_id=actor_id, action="resource.registered",
                        resource_type="resource", resource_id=resource_id,
                        detail={"kind": kind, "owner_id": owner_id, "label": label})
            return self._store_receipt(conn, request_id=request_id, action="register_resource",
                                      payload=payload, resource_type="resource",
                                      resource_id=resource_id, response={"resource_id": resource_id})

    def _validate_capabilities(self, kind: str, caps: dict[str, Any]) -> None:
        if not isinstance(caps, dict):
            raise ValidationError("capabilities 必须是对象")
        if kind in ("venue_window", "display_case"):
            self._text(caps.get("city", ""), "capabilities.city", 80)
        if kind == "venue_window":
            start = self._dt(caps.get("window_start", ""), "capabilities.window_start")
            end = self._dt(caps.get("window_end", ""), "capabilities.window_end")
            if end <= start:
                raise ValidationError("场馆窗口结束必须晚于开始")
        if kind in ("vehicle", "insurance_quota"):
            if not isinstance(caps.get("capacity"), (int, float)) or caps["capacity"] <= 0:
                raise ValidationError("capacity 必须为正数")
        if kind == "insurance_quota":
            self._text(caps.get("currency", ""), "capabilities.currency", 8)

    def upsert_qualification(self, *, request_id: str, actor_id: str, venue_organization_id: str,
                             artwork_id: str, qualified: bool, certificate: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "venue_organization_id": venue_organization_id,
                   "artwork_id": artwork_id, "qualified": qualified}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            if conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?", (artwork_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            if actor.role not in (ROLE_OPERATOR, ROLE_VENUE):
                raise PermissionDenied("当前角色不能维护接收资质")
            if actor.role == ROLE_VENUE and actor.organization_id != venue_organization_id:
                raise PermissionDenied("只能维护本机构的接收资质")
            venue_organization_id = self._id(venue_organization_id, "venue_organization_id")
            replay = self._replay(conn, request_id=request_id, action="upsert_qualification",
                                  payload=payload)
            if replay:
                return replay

            conn.execute(
                "INSERT INTO receiver_qualifications(venue_organization_id,artwork_id,qualified,"
                "certificate,updated_by,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(venue_organization_id,artwork_id) DO UPDATE SET "
                "qualified=excluded.qualified,certificate=excluded.certificate,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (venue_organization_id, artwork_id, 1 if qualified else 0, str(certificate)[:200],
                 actor_id, self._now()),
            )
            self._audit(conn, actor_id=actor_id, action="qualification.updated",
                        resource_type="artwork", resource_id=artwork_id,
                        detail={"venue_organization_id": venue_organization_id,
                                "qualified": bool(qualified)})
            return self._store_receipt(
                conn, request_id=request_id, action="upsert_qualification", payload=payload,
                resource_type="qualification",
                resource_id=f"{venue_organization_id}:{artwork_id}",
                response={"venue_organization_id": venue_organization_id, "artwork_id": artwork_id})

    # ------------------------------------------------------------------
    # 计划版本：草案创建
    # ------------------------------------------------------------------
    def create_plan_version(self, *, request_id: str, actor_id: str, plan_id: str, artwork_id: str,
                            components: list[str], segments: list[dict[str, Any]],
                            sessions: dict[str, list[dict[str, Any]]] | None = None,
                            commitments: dict[str, list[dict[str, Any]]] | None = None,
                            lease_minutes: int = DEFAULT_LEASE_MINUTES) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "artwork_id": artwork_id,
                   "components": components, "segments": segments, "sessions": sessions or {},
                   "commitments": commitments or {}, "lease_minutes": lease_minutes}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR)
            plan_id = self._id(plan_id, "plan_id")
            replay = self._replay(conn, request_id=request_id, action="create_plan_version",
                                  payload=payload)
            if replay:
                return replay
            if conn.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone() is not None:
                raise ConflictError("计划已存在；重排请使用 rework_plan")
            artwork = self._get_artwork(conn, artwork_id)
            self._validate_lease(lease_minutes)
            prepared, resources = self._prepare_segments(conn, segments_in=segments, artwork=artwork)
            for component in components:
                if component not in artwork["components"]:
                    raise ValidationError(f"组件 {component} 未在作品清单中登记")
            if not components:
                raise ValidationError("巡展至少包含一个组件")
            self._validate_itinerary(prepared)
            session_rows, commitment_rows = self._validate_publicity(prepared, sessions or {},
                                                                      commitments or {})
            self._check_resource_availability(conn, prepared, resources, ignore_segments=set())
            version = 1
            self._persist_draft(
                conn, actor=actor, plan_id=plan_id, version=version, artwork=artwork,
                components=components, prepared=prepared, session_rows=session_rows,
                commitment_rows=commitment_rows, lease_minutes=lease_minutes,
                base_version=None, reason=None, change_summary=None, replacement_map={})
            self._audit(conn, actor_id=actor_id, action="plan.draft_created",
                        resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                        detail={"segment_count": len(prepared)})
            return self._store_receipt(
                conn, request_id=request_id, action="create_plan_version", payload=payload,
                resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                response={"plan_id": plan_id, "version": version})

    def _validate_lease(self, lease_minutes: int) -> None:
        if not isinstance(lease_minutes, int) or not (1 <= lease_minutes <= MAX_LEASE_MINUTES):
            raise ValidationError(f"lease_minutes 必须在 1..{MAX_LEASE_MINUTES} 之间")

    def _get_artwork(self, conn, artwork_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM artworks WHERE artwork_id=?", (artwork_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return {"artwork_id": row["artwork_id"], "title": row["title"],
                "representative_id": row["representative_id"],
                "components": tuple(self._loads(row["components_json"])),
                "condition_note": row["condition_note"]}

    # ------------------------------------------------------------------
    # 区段准备与校验
    # ------------------------------------------------------------------
    def _prepare_segments(self, conn, *, segments_in: list[dict[str, Any]],
                          artwork: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict]]:
        if not isinstance(segments_in, list) or not segments_in:
            raise ValidationError("segments 必须是非空列表")
        prepared: list[dict[str, Any]] = []
        resources: dict[str, dict] = {}
        seen_ids: set[str] = set()
        for raw in segments_in:
            if not isinstance(raw, dict):
                raise ValidationError("区段必须是对象")
            segment_id = self._id(raw.get("segment_id", ""), "segment_id")
            if segment_id in seen_ids:
                raise ValidationError(f"区段 {segment_id} 重复")
            seen_ids.add(segment_id)
            kind = str(raw.get("kind", "")).strip()
            if kind not in SEGMENT_KINDS:
                raise ValidationError("区段种类不支持")
            start = self._dt(raw.get("start_at", ""), "start_at")
            end = self._dt(raw.get("end_at", ""), "end_at")
            if end <= start:
                raise ValidationError(f"区段 {segment_id} 结束必须晚于开始")
            requirement = raw.get("requirement") or {}
            if not isinstance(requirement, dict):
                raise ValidationError("requirement 必须是对象")
            alternatives = raw.get("alternatives") or []
            if not isinstance(alternatives, list):
                raise ValidationError("alternatives 必须是列表")
            alternatives = tuple(self._id(a, "alternatives") for a in alternatives)

            resource = self._load_resource(conn, raw.get("resource_id"), resources)
            if resource["kind"] != RESOURCE_KIND_FOR_SEGMENT[kind]:
                raise ValidationError(f"区段 {segment_id} 的资源种类不匹配")
            holds = [(resource["resource_id"], self._ts(start), self._ts(end))]
            if kind == SEG_VENUE:
                self._validate_venue_requirement(conn, artwork, resource, requirement, start, end,
                                                 resources)
                holds.append((requirement["case_resource_id"], self._ts(start), self._ts(end)))
            elif kind == SEG_TRANSPORT:
                self._validate_transport_requirement(resource, requirement)
            else:
                self._validate_insurance_requirement(resource, requirement)

            prepared.append({
                "segment_id": segment_id,
                "kind": kind,
                "start_dt": start,
                "end_dt": end,
                "start_at": self._ts(start),
                "end_at": self._ts(end),
                "resource_id": resource["resource_id"],
                "requirement": requirement,
                "alternatives": alternatives,
                "status": SEG_PENDING,
                "holds": holds,
                "confirm_roles": CONFIRM_ROLES_FOR_SEGMENT[kind],
                "carried": False,
            })
        prepared.sort(key=lambda s: (s["start_dt"], s["end_dt"], s["segment_id"]))
        return prepared, resources

    def _load_resource(self, conn, resource_id: Any, cache: dict[str, dict]) -> dict:
        resource_id = self._id(resource_id or "", "resource_id")
        if resource_id not in cache:
            row = conn.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"资源 {resource_id} 不存在")
            cache[resource_id] = {"resource_id": row["resource_id"], "kind": row["kind"],
                                  "owner_id": row["owner_id"], "label": row["label"],
                                  "capabilities": self._loads(row["capabilities_json"])}
        return cache[resource_id]

    def _owner_org(self, conn, participant_id: str) -> str:
        return conn.execute("SELECT organization_id FROM participants WHERE participant_id=?",
                            (participant_id,)).fetchone()["organization_id"]

    def _validate_venue_requirement(self, conn, artwork: dict, resource: dict, requirement: dict,
                                    start: datetime, end: datetime, resources: dict) -> None:
        caps = resource["capabilities"]
        if start < self._dt(caps["window_start"], "window_start") or \
                end > self._dt(caps["window_end"], "window_end"):
            raise ValidationError(f"场馆区段必须落在 {resource['resource_id']} 的开放窗口内")
        install_hours = requirement.get("install_hours")
        dismantle_hours = requirement.get("dismantle_hours")
        if not isinstance(install_hours, (int, float)) or install_hours <= 0:
            raise ValidationError("install_hours 必须为正数")
        if not isinstance(dismantle_hours, (int, float)) or dismantle_hours <= 0:
            raise ValidationError("dismantle_hours 必须为正数")
        if install_hours + dismantle_hours > (end - start).total_seconds() / 3600 + 1e-9:
            raise ValidationError("场馆窗口不足以容纳布撤展工时")
        case = self._load_resource(conn, requirement.get("case_resource_id"), resources)
        if case["kind"] != "display_case":
            raise ValidationError("case_resource_id 必须是展柜资源")
        if case["capabilities"].get("city") != caps.get("city"):
            raise ValidationError("展柜与场馆窗口不在同一城市")
        venue_org = self._owner_org(conn, resource["owner_id"])
        if self._owner_org(conn, case["owner_id"]) != venue_org:
            raise ValidationError("展柜必须属于接收场馆所属机构")
        qual = conn.execute(
            "SELECT qualified,certificate FROM receiver_qualifications "
            "WHERE venue_organization_id=? AND artwork_id=?",
            (venue_org, artwork["artwork_id"]),
        ).fetchone()
        if qual is None or not qual["qualified"]:
            raise ValidationError(
                f"场馆 {venue_org} 缺少作品 {artwork['artwork_id']} 的有效接收资质")
        requirement["_city"] = caps.get("city")
        requirement["_venue_organization_id"] = venue_org
        requirement["_certificate"] = qual["certificate"]

    def _validate_transport_requirement(self, resource: dict, requirement: dict) -> None:
        self._text(requirement.get("from_city", ""), "from_city", 80)
        self._text(requirement.get("to_city", ""), "to_city", 80)
        load = requirement.get("load_volume")
        if load is not None:
            if not isinstance(load, (int, float)) or load <= 0:
                raise ValidationError("load_volume 必须为正数")
            if load > resource["capabilities"]["capacity"]:
                raise ValidationError("承运能力不足")

    def _validate_insurance_requirement(self, resource: dict, requirement: dict) -> None:
        amount = requirement.get("coverage_amount")
        if not isinstance(amount, (int, float)) or amount <= 0:
            raise ValidationError("coverage_amount 必须为正数")
        currency = self._text(requirement.get("currency", ""), "currency", 8)
        if currency != resource["capabilities"]["currency"]:
            raise ValidationError("投保险种与额度币种不一致")

    def _validate_itinerary(self, prepared: list[dict]) -> None:
        venues = [s for s in prepared if s["kind"] == SEG_VENUE]
        transports = [s for s in prepared if s["kind"] == SEG_TRANSPORT]
        insurances = [s for s in prepared if s["kind"] == SEG_INSURANCE]
        if not venues:
            raise ValidationError("计划至少包含一个场馆区段")
        if len(transports) != len(venues) - 1:
            raise ValidationError("运输区段必须恰好连接相邻场馆（场馆数 - 1）")
        if len(insurances) != 1:
            raise ValidationError("计划必须包含一个覆盖全程的保险区段")
        previous_end: datetime | None = None
        for seg in prepared:
            # 保险区段按设计覆盖全程，只做覆盖校验，不参与相邻不重叠检查。
            if seg["kind"] == SEG_INSURANCE:
                continue
            if previous_end is not None and seg["start_dt"] < previous_end:
                raise ValidationError("区段时间不得重叠")
            previous_end = seg["end_dt"]
        for index, transport in enumerate(transports):
            dep, arr = venues[index], venues[index + 1]
            if transport["requirement"]["from_city"] != dep["requirement"]["_city"]:
                raise ValidationError(f"运输 {transport['segment_id']} 起点与场馆城市不符")
            if transport["requirement"]["to_city"] != arr["requirement"]["_city"]:
                raise ValidationError(f"运输 {transport['segment_id']} 终点与场馆城市不符")
            if not (dep["end_dt"] <= transport["start_dt"] <= arr["start_dt"]):
                raise ValidationError(f"运输 {transport['segment_id']} 必须在相邻场馆之间")
        insurance = insurances[0]
        if insurance["start_dt"] > venues[0]["start_dt"] or \
                insurance["end_dt"] < venues[-1]["end_dt"]:
            raise ValidationError("保险区段必须覆盖全部场馆时段")

    def _validate_publicity(self, prepared: list[dict], sessions_in: dict[str, Any],
                            commitments_in: dict[str, Any]
                            ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        venue_ids = {s["segment_id"] for s in prepared if s["kind"] == SEG_VENUE}
        for mapping, name in ((sessions_in, "sessions"), (commitments_in, "commitments")):
            if not isinstance(mapping, dict):
                raise ValidationError(f"{name} 必须以区段编号为键")
            unknown = set(mapping) - venue_ids
            if unknown:
                raise ValidationError(f"{name} 引用了不存在或非场馆的区段：{sorted(unknown)}")
        session_rows: dict[str, list[dict]] = {}
        for seg in prepared:
            if seg["kind"] != SEG_VENUE:
                continue
            items = sessions_in.get(seg["segment_id"], [])
            if not isinstance(items, list) or not items:
                raise ValidationError(f"场馆区段 {seg['segment_id']} 至少安排一个观众场次")
            rows = []
            for item in items:
                starts = self._dt(item.get("starts_at", ""), "session.starts_at")
                if not (seg["start_dt"] <= starts <= seg["end_dt"]):
                    raise ValidationError("观众场次必须在场馆区段时段内")
                capacity = item.get("capacity")
                if not isinstance(capacity, int) or capacity < 0:
                    raise ValidationError("场次 capacity 必须是非负整数")
                rows.append({"session_id": uuid.uuid4().hex, "starts_at": self._ts(starts),
                             "label": self._text(item.get("label", ""), "session.label", 120),
                             "capacity": capacity})
            session_rows[seg["segment_id"]] = rows
        commitment_rows: dict[str, list[dict]] = {}
        for seg_id, items in commitments_in.items():
            rows = []
            for item in items or []:
                rows.append({"commitment_id": uuid.uuid4().hex,
                             "channel": self._text(item.get("channel", ""), "commitment.channel", 80),
                             "promised_at": self._ts(self._dt(item.get("promised_at", ""),
                                                              "commitment.promised_at")),
                             "detail": self._text(item.get("detail", ""), "commitment.detail", 300)})
            commitment_rows[seg_id] = rows
        return session_rows, commitment_rows

    def _active_holds(self, conn, *, resource_id: str, start_at: str, end_at: str,
                      ignore_segments: set[str]) -> list:
        join = ("JOIN segments s ON s.segment_id=h.segment_id "
                "AND s.plan_id=h.plan_id AND s.version=h.version ")
        if ignore_segments:
            placeholders = ",".join("?" * len(ignore_segments))
            sql = ("SELECT h.segment_id,h.plan_id,h.version,h.start_at,h.end_at,s.requirement_json "
                   "FROM resource_holds h " + join +
                   f"WHERE h.resource_id=? AND h.status IN ('held','committed') "
                   f"AND h.start_at < ? AND h.end_at > ? AND h.segment_id NOT IN ({placeholders})")
            params = [resource_id, end_at, start_at, *ignore_segments]
        else:
            sql = ("SELECT h.segment_id,h.plan_id,h.version,h.start_at,h.end_at,s.requirement_json "
                   "FROM resource_holds h " + join +
                   "WHERE h.resource_id=? AND h.status IN ('held','committed') "
                   "AND h.start_at < ? AND h.end_at > ?")
            params = [resource_id, end_at, start_at]
        return conn.execute(sql, params).fetchall()

    def _check_resource_availability(self, conn, prepared: list[dict], resources: dict[str, dict],
                                     ignore_segments: set[str]) -> None:
        """窗口/展柜/车辆做区间互斥；保险额度做同时段投保金额求和。"""

        blockers: list[dict[str, Any]] = []
        for seg in prepared:
            if seg.get("carried"):
                continue
            for resource_id, start_at, end_at in seg["holds"]:
                kind = resources[resource_id]["kind"]
                active = self._active_holds(conn, resource_id=resource_id, start_at=start_at,
                                            end_at=end_at, ignore_segments=ignore_segments)
                if kind == "insurance_quota":
                    if resource_id != seg["resource_id"]:
                        continue
                    used = float(seg["requirement"]["coverage_amount"])
                    used += sum(float(self._loads(h["requirement_json"]).get("coverage_amount", 0))
                                for h in active)
                    capacity = float(resources[resource_id]["capabilities"]["capacity"])
                    if used > capacity + 1e-9:
                        blockers.append({"resource_id": resource_id, "kind": kind, "capacity": capacity,
                                         "requested_total": used,
                                         "blocked_by": [h["segment_id"] for h in active]})
                elif active:
                    blockers.append({"resource_id": resource_id, "kind": kind,
                                     "start_at": start_at, "end_at": end_at,
                                     "blocked_by": [{"segment_id": h["segment_id"], "plan_id": h["plan_id"],
                                                     "version": h["version"]} for h in active]})
        if blockers:
            err = ConflictError("资源在请求时段已被占用")
            err.blockers = blockers  # type: ignore[attr-defined]
            raise err

    # ------------------------------------------------------------------
    # 草案落库（初版与重排版共用）
    # ------------------------------------------------------------------
    def _persist_draft(self, conn, *, actor: Participant, plan_id: str, version: int,
                       artwork: dict, components: list[str], prepared: list[dict],
                       session_rows: dict[str, list[dict]], commitment_rows: dict[str, list[dict]],
                       lease_minutes: int, base_version: int | None, reason: str | None,
                       change_summary: str | None, replacement_map: dict[str, dict]) -> datetime:
        now = self._now_dt()
        lease_expires = now + timedelta(minutes=lease_minutes)
        conn.execute(
            "INSERT INTO plans(plan_id,artwork_id,current_version,published_version,created_at) "
            "VALUES(?,?,?,NULL,?) ON CONFLICT(plan_id) DO UPDATE SET current_version=excluded.current_version",
            (plan_id, artwork["artwork_id"], version, self._ts(now)),
        )
        conn.execute(
            "INSERT INTO plan_versions(plan_id,version,status,components_json,created_by,created_at,"
            "lease_expires_at,rework_of_version,rework_reason,change_summary,parent_segment_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, version, PLAN_DRAFT, self._jdump(components), actor.participant_id,
             self._ts(now), self._ts(lease_expires), base_version, reason, change_summary,
             next(iter(replacement_map), None)),
        )
        for index, seg in enumerate(prepared):
            if seg.get("carried"):
                # 携带的是已发布且未受影响的区段：保留交接事实，不重复占用、不重复待办。
                conn.execute(
                    "INSERT INTO segments(segment_id,plan_id,version,kind,start_at,end_at,resource_id,"
                    "requirement_json,status,alternatives_json,confirmed_at,published_at,"
                    "handover_at,handover_by,handover_ref) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (seg["segment_id"], plan_id, version, seg["kind"], seg["start_at"], seg["end_at"],
                     seg["resource_id"], self._jdump(seg["requirement"]), seg["status"],
                     self._jdump(seg.get("alternatives", ())), seg.get("confirmed_at"),
                     seg.get("published_at"), seg.get("handover_at"), seg.get("handover_by"),
                     seg.get("handover_ref")),
                )
                continue
            conn.execute(
                "INSERT INTO segments(segment_id,plan_id,version,kind,start_at,end_at,resource_id,"
                "requirement_json,status,alternatives_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (seg["segment_id"], plan_id, version, seg["kind"], seg["start_at"], seg["end_at"],
                 seg["resource_id"], self._jdump(seg["requirement"]), SEG_PENDING,
                 self._jdump(seg.get("alternatives", ()))),
            )
            for hold_resource_id, hold_start, hold_end in seg["holds"]:
                conn.execute(
                    "INSERT INTO resource_holds(hold_id,resource_id,plan_id,version,segment_id,"
                    "start_at,end_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, hold_resource_id, plan_id, version, seg["segment_id"],
                     hold_start, hold_end, "held", self._ts(now)),
                )
            for role in seg["confirm_roles"]:
                conn.execute(
                    "INSERT INTO todos(todo_id,plan_id,version,role,segment_id,segment_kind,"
                    "action_state,created_at,sequence) VALUES(?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, plan_id, version, role, seg["segment_id"], seg["kind"],
                     "pending", self._ts(now), index * 10 + ROLE_ORDER[role]),
                )
            for srow in session_rows.get(seg["segment_id"], ()):
                conn.execute(
                    "INSERT INTO audience_sessions(session_id,plan_id,version,segment_id,starts_at,"
                    "label,capacity,publicity_state) VALUES(?,?,?,?,?,?,?,?)",
                    (srow["session_id"], plan_id, version, seg["segment_id"], srow["starts_at"],
                     srow["label"], srow["capacity"], "scheduled"),
                )
            for crow in commitment_rows.get(seg["segment_id"], ()):
                conn.execute(
                    "INSERT INTO publicity_commitments(commitment_id,plan_id,version,venue_segment_id,"
                    "channel,promised_at,detail,status) VALUES(?,?,?,?,?,?,?,?)",
                    (crow["commitment_id"], plan_id, version, seg["segment_id"], crow["channel"],
                     crow["promised_at"], crow["detail"], "promised_pending"),
                )
        self._timeline(conn, plan_id, version, "version_created", actor.participant_id,
                       {"base_version": base_version, "rework_reason": reason,
                        "change_summary": change_summary,
                        "replaces_segment_ids": sorted(replacement_map),
                        "segment_ids": [s["segment_id"] for s in prepared],
                        "lease_expires_at": self._ts(lease_expires)})
        return lease_expires

    # ------------------------------------------------------------------
    # 草案过期
    # ------------------------------------------------------------------
    def _get_version(self, conn, plan_id: str, version: int) -> dict:
        row = conn.execute("SELECT * FROM plan_versions WHERE plan_id=? AND version=?",
                           (plan_id, version)).fetchone()
        if row is None:
            raise NotFoundError("计划版本不存在")
        return dict(row)

    def _latest_version(self, conn, plan_id: str) -> int:
        row = conn.execute("SELECT current_version FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("计划不存在")
        return int(row["current_version"])

    def _expire_version_conn(self, conn, plan_id: str, version: int, *, reason: str) -> None:
        conn.execute("UPDATE plan_versions SET status=? WHERE plan_id=? AND version=? AND status=?",
                     (PLAN_EXPIRED, plan_id, version, PLAN_DRAFT))
        conn.execute("DELETE FROM resource_holds WHERE plan_id=? AND version=? AND status='held'",
                     (plan_id, version))
        conn.execute("UPDATE todos SET action_state='voided' WHERE plan_id=? AND version=? "
                     "AND action_state='pending'", (plan_id, version))
        conn.execute("UPDATE audience_sessions SET publicity_state='withdrawn' "
                     "WHERE plan_id=? AND version=? AND publicity_state='scheduled'", (plan_id, version))
        conn.execute("UPDATE publicity_commitments SET status='voided' "
                     "WHERE plan_id=? AND version=? AND status='promised_pending'", (plan_id, version))
        self._timeline(conn, plan_id, version, "version_expired", "system", {"reason": reason})
        self._audit(conn, actor_id="system", action="plan.expired",
                    resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                    detail={"reason": reason})

    def _sweep_conn(self, conn) -> list[str]:
        """在当前事务内释放所有到期草案的租约。"""

        expired: list[str] = []
        now = self._now()
        rows = conn.execute("SELECT plan_id,version FROM plan_versions WHERE status=? "
                            "AND lease_expires_at<=?", (PLAN_DRAFT, now)).fetchall()
        for row in rows:
            self._expire_version_conn(conn, row["plan_id"], row["version"], reason="lease_expired")
            expired.append(f"{row['plan_id']}/v{row['version']}")
        return expired

    def sweep_expired(self, *, actor_id: str = "system") -> dict[str, Any]:
        """清理所有到期草案；可由定时器或任意写请求触发。"""

        with self.database.transaction(immediate=True) as conn:
            return {"expired": self._sweep_conn(conn)}

    # ------------------------------------------------------------------
    # 确认 / 拒绝 / 发布
    # ------------------------------------------------------------------
    def _authorize_todo(self, conn, actor: Participant, plan_id: str, version: int,
                        todo: dict) -> None:
        """参与方只能确认本机构资源（或本人代表作品）的待办。"""

        if actor.role == ROLE_REPRESENTATIVE:
            rep = conn.execute(
                "SELECT a.representative_id FROM plans p JOIN artworks a ON a.artwork_id=p.artwork_id "
                "WHERE p.plan_id=?", (plan_id,)).fetchone()
            if rep["representative_id"] != actor.participant_id:
                raise PermissionDenied("只能确认本人代表作品的计划")
            return
        if actor.role == ROLE_OPERATOR:
            return
        if actor.role != todo["role"]:
            raise PermissionDenied("当前角色与待办职责不匹配")
        owner = conn.execute(
            "SELECT p.organization_id FROM resources r JOIN participants p ON p.participant_id=r.owner_id "
            "JOIN segments s ON s.resource_id=r.resource_id AND s.plan_id=? AND s.version=? "
            "WHERE s.segment_id=?",
            (plan_id, version, todo["segment_id"])).fetchone()
        if owner is None or owner["organization_id"] != actor.organization_id:
            raise PermissionDenied("只能确认本机构负责资源的待办")

    def respond(self, *, request_id: str, actor_id: str, plan_id: str, version: int | None,
                segment_id: str, decision: str, note: str = "") -> WriteReceipt:
        plan_id = self._id(plan_id, "plan_id")
        segment_id = self._id(segment_id, "segment_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "version": version,
                   "segment_id": segment_id, "decision": decision, "note": note}
        if decision not in ("confirm", "reject"):
            raise ValidationError("decision 必须是 confirm 或 reject")
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            replay = self._replay(conn, request_id=request_id, action=f"respond_{decision}",
                                  payload=payload)
            if replay:
                return replay
            version = version or self._latest_version(conn, plan_id)
            self._sweep_conn(conn)
            ver = self._get_version(conn, plan_id, version)
            if ver["status"] != PLAN_DRAFT:
                raise StateError(f"版本当前状态为 {ver['status']}，不能再确认")
            todo = conn.execute(
                "SELECT * FROM todos WHERE plan_id=? AND version=? AND segment_id=? AND role=?",
                (plan_id, version, segment_id, actor.role),
            ).fetchone()
            if todo is None:
                raise NotFoundError("当前参与方对该区段没有待确认事项")
            todo = dict(todo)
            if todo["action_state"] == decision:
                # 重复确认/重复拒绝：幂等返回，不新增任何占用。
                return WriteReceipt(request_id, "todo", todo["todo_id"], True)
            if todo["action_state"] in ("confirmed", "rejected"):
                raise StateError("该区段已经给出不同结论，不能更改")
            self._authorize_todo(conn, actor, plan_id, version, todo)

            if decision == "reject":
                conn.execute("UPDATE todos SET action_state='rejected',decided_by=?,decided_at=? "
                             "WHERE todo_id=?", (actor_id, self._now(), todo["todo_id"]))
                conn.execute("UPDATE plan_versions SET status=?,rejected_by=?,reject_reason=? "
                             "WHERE plan_id=? AND version=?",
                             (PLAN_REJECTED, actor_id, self._note(note, 500),
                              plan_id, version))
                # 仅释放“未全部确认”区段（含被拒区段）的草案租约；
                # 已由各方确认的区段保留租约，重排时原样携带。
                unconfirmed_sql = (
                    "SELECT segment_id FROM todos WHERE plan_id=? AND version=? "
                    "GROUP BY segment_id HAVING COUNT(*) != "
                    "SUM(CASE WHEN action_state='confirmed' THEN 1 ELSE 0 END)")
                conn.execute(
                    "DELETE FROM resource_holds WHERE plan_id=? AND version=? AND status='held' "
                    f"AND segment_id IN ({unconfirmed_sql})",
                    (plan_id, version, plan_id, version))
                conn.execute("UPDATE todos SET action_state='voided' WHERE plan_id=? AND version=? "
                             "AND action_state='pending'", (plan_id, version))
                conn.execute(
                    "UPDATE audience_sessions SET publicity_state='withdrawn' "
                    "WHERE plan_id=? AND version=? AND publicity_state='scheduled' "
                    f"AND segment_id IN ({unconfirmed_sql})",
                    (plan_id, version, plan_id, version))
                conn.execute(
                    "UPDATE publicity_commitments SET status='voided' "
                    "WHERE plan_id=? AND version=? AND status='promised_pending' "
                    f"AND venue_segment_id IN ({unconfirmed_sql})",
                    (plan_id, version, plan_id, version))
                self._timeline(conn, plan_id, version, "segment_rejected", actor_id,
                               {"segment_id": segment_id, "role": actor.role, "note": note})
                self._audit(conn, actor_id=actor_id, action="plan.segment_rejected",
                            resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                            detail={"segment_id": segment_id, "role": actor.role})
                return self._store_receipt(
                    conn, request_id=request_id, action="respond_reject", payload=payload,
                    resource_type="todo", resource_id=todo["todo_id"],
                    response={"todo_id": todo["todo_id"], "status": PLAN_REJECTED})

            conn.execute("UPDATE todos SET action_state='confirmed',decided_by=?,decided_at=? "
                         "WHERE todo_id=?", (actor_id, self._now(), todo["todo_id"]))
            self._timeline(conn, plan_id, version, "segment_confirmed", actor_id,
                           {"segment_id": segment_id, "role": actor.role})
            self._audit(conn, actor_id=actor_id, action="plan.segment_confirmed",
                        resource_type="todo", resource_id=todo["todo_id"],
                        detail={"segment_id": segment_id, "role": actor.role})
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM todos WHERE plan_id=? AND version=? AND action_state='pending'",
                (plan_id, version)).fetchone()["c"]
            new_status = PLAN_CONFIRMED if remaining == 0 else PLAN_DRAFT
            if remaining == 0:
                conn.execute("UPDATE plan_versions SET status=?,confirmed_at=? WHERE plan_id=? AND version=?",
                             (PLAN_CONFIRMED, self._now(), plan_id, version))
            conn.execute("UPDATE segments SET confirmed_at=COALESCE(confirmed_at,?) "
                         "WHERE segment_id=? AND confirmed_at IS NULL", (self._now(), segment_id))
            return self._store_receipt(
                conn, request_id=request_id, action="respond_confirm", payload=payload,
                resource_type="todo", resource_id=todo["todo_id"],
                response={"todo_id": todo["todo_id"], "status": new_status, "remaining": remaining})

    def publish(self, *, request_id: str, actor_id: str, plan_id: str,
                version: int | None = None) -> WriteReceipt:
        plan_id = self._id(plan_id, "plan_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "version": version}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR)
            replay = self._replay(conn, request_id=request_id, action="publish_plan", payload=payload)
            if replay:
                return replay
            version = version or self._latest_version(conn, plan_id)
            self._sweep_conn(conn)
            ver = self._get_version(conn, plan_id, version)
            if ver["status"] not in (PLAN_DRAFT, PLAN_CONFIRMED):
                raise StateError(f"版本状态为 {ver['status']}，不能发布")
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM todos WHERE plan_id=? AND version=? AND action_state='pending'",
                (plan_id, version)).fetchone()["c"]
            if pending:
                raise StateError("仍有参与方未确认，不能发布")

            now = self._now()
            plan = conn.execute("SELECT published_version FROM plans WHERE plan_id=?",
                                (plan_id,)).fetchone()
            old_version = plan["published_version"]
            new_segment_ids = {r["segment_id"] for r in conn.execute(
                "SELECT segment_id FROM segments WHERE plan_id=? AND version=?", (plan_id, version))}
            impacted_sessions: list[dict[str, Any]] = []
            carried_commitments: list[str] = []
            if old_version is not None:
                old_ids = {r["segment_id"] for r in conn.execute(
                    "SELECT segment_id FROM segments WHERE plan_id=? AND version=?",
                    (plan_id, old_version))}
                carried = old_ids & new_segment_ids
                replaced = old_ids - carried
                conn.execute("UPDATE plan_versions SET status=?,superseded_at=? WHERE plan_id=? AND version=?",
                             (PLAN_SUPERSEDED, now, plan_id, old_version))
                # 仅受影响区段释放物理租约（携带区段的租约按原 segment_id 继续生效）。
                if replaced:
                    placeholders = ",".join("?" * len(replaced))
                    conn.execute(
                        f"DELETE FROM resource_holds WHERE plan_id=? AND status IN ('held','committed') "
                        f"AND segment_id IN ({placeholders})", (plan_id, *replaced))
                # 公开场次只能标注 shifted/cancelled，不能删除或静默改期。
                old_venues = [r["segment_id"] for r in conn.execute(
                    "SELECT segment_id FROM segments WHERE plan_id=? AND version=? AND kind='venue' "
                    "ORDER BY start_at,segment_id", (plan_id, old_version))]
                new_venues = [r["segment_id"] for r in conn.execute(
                    "SELECT segment_id FROM segments WHERE plan_id=? AND version=? AND kind='venue' "
                    "ORDER BY start_at,segment_id", (plan_id, version))]
                for position, old_sid in enumerate(old_venues):
                    if old_sid in carried:
                        continue
                    new_sid = new_venues[position] if position < len(new_venues) else None
                    state = "shifted" if new_sid else "cancelled"
                    old_sessions = conn.execute(
                        "SELECT * FROM audience_sessions WHERE segment_id=? ORDER BY starts_at,label",
                        (old_sid,)).fetchall()
                    new_sessions = conn.execute(
                        "SELECT * FROM audience_sessions WHERE segment_id=? ORDER BY starts_at,label",
                        (new_sid,)).fetchall() if new_sid else []
                    for idx, old_session in enumerate(old_sessions):
                        new_starts = new_sessions[idx]["starts_at"] if idx < len(new_sessions) else None
                        conn.execute("UPDATE audience_sessions SET publicity_state=? WHERE session_id=?",
                                     (state, old_session["session_id"]))
                        impacted_sessions.append({
                            "session_id": old_session["session_id"], "segment_id": old_sid,
                            "label": old_session["label"], "old_starts_at": old_session["starts_at"],
                            "new_starts_at": new_starts, "state": state,
                        })
                if replaced:
                    for old_commit in conn.execute(
                            "SELECT commitment_id,venue_segment_id FROM publicity_commitments "
                            "WHERE plan_id=? AND version=? AND status='promised' AND venue_segment_id IN ("
                            + ",".join("?" * len(replaced)) + ")",
                            (plan_id, old_version, *replaced)):
                        conn.execute("UPDATE publicity_commitments SET status='carried' WHERE commitment_id=?",
                                     (old_commit["commitment_id"],))
                        carried_commitments.append(old_commit["commitment_id"])
                self._timeline(conn, plan_id, old_version, "version_superseded", actor_id,
                               {"by_version": version, "replaced_segment_ids": sorted(replaced),
                                "impacted_sessions": impacted_sessions})

            conn.execute("UPDATE plan_versions SET status=?,published_at=?,lease_expires_at=NULL "
                         "WHERE plan_id=? AND version=?", (PLAN_PUBLISHED, now, plan_id, version))
            conn.execute("UPDATE segments SET published_at=COALESCE(published_at,?) "
                         "WHERE plan_id=? AND version=?", (now, plan_id, version))
            conn.execute("UPDATE resource_holds SET status='committed' WHERE plan_id=? AND version=?",
                         (plan_id, version))
            conn.execute("UPDATE audience_sessions SET publicity_state='announced',announced_at=? "
                         "WHERE plan_id=? AND version=?", (now, plan_id, version))
            conn.execute("UPDATE publicity_commitments SET status='promised' "
                         "WHERE plan_id=? AND version=? AND status='promised_pending'",
                         (plan_id, version))
            if version > 1:
                # 紧邻底座（可能从未发布，例如被拒后重排）上携带到本版本的场馆区段，
                # 其沿用的场次/承诺行也随本次发布生效。
                carried_in_version = {r["segment_id"] for r in conn.execute(
                    "SELECT segment_id FROM segments WHERE plan_id=? AND version=?",
                    (plan_id, version - 1))} & new_segment_ids
                if carried_in_version:
                    placeholders = ",".join("?" * len(carried_in_version))
                    conn.execute(
                        f"UPDATE audience_sessions SET publicity_state='announced',announced_at=? "
                        f"WHERE segment_id IN ({placeholders}) AND publicity_state='scheduled'",
                        (now, *carried_in_version))
                    conn.execute(
                        f"UPDATE publicity_commitments SET status='promised' "
                        f"WHERE venue_segment_id IN ({placeholders}) AND status='promised_pending'",
                        tuple(carried_in_version))
            conn.execute("UPDATE plans SET published_version=? WHERE plan_id=?", (version, plan_id))
            self._timeline(conn, plan_id, version, "version_published", actor_id,
                           {"superseded_version": old_version,
                            "impacted_sessions": impacted_sessions,
                            "carried_commitments": carried_commitments})
            self._audit(conn, actor_id=actor_id, action="plan.published",
                        resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                        detail={"superseded_version": old_version,
                                "impacted_session_count": len(impacted_sessions)})
            return self._store_receipt(
                conn, request_id=request_id, action="publish_plan", payload=payload,
                resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                response={"plan_id": plan_id, "version": version,
                          "superseded_version": old_version,
                          "impacted_sessions": impacted_sessions})

    def abort_version(self, *, request_id: str, actor_id: str, plan_id: str,
                      version: int | None = None, reason: str = "") -> WriteReceipt:
        """运营方中止草案/被拒版本，释放其全部资源租约；已发布版本不能中止。"""

        plan_id = self._id(plan_id, "plan_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "version": version, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR)
            replay = self._replay(conn, request_id=request_id, action="abort_version",
                                  payload=payload)
            if replay:
                return replay
            version = version or self._latest_version(conn, plan_id)
            ver = self._get_version(conn, plan_id, version)
            if ver["status"] not in (PLAN_DRAFT, PLAN_REJECTED):
                raise StateError(f"版本状态为 {ver['status']}，不能中止")
            # 释放该未发布版本的全部租约（含被拒后保留的已确认区段）。
            conn.execute("DELETE FROM resource_holds WHERE plan_id=? AND version=?",
                         (plan_id, version))
            conn.execute("UPDATE todos SET action_state='voided' WHERE plan_id=? AND version=? "
                         "AND action_state IN ('pending','confirmed','rejected')",
                         (plan_id, version))
            conn.execute("UPDATE audience_sessions SET publicity_state='withdrawn' "
                         "WHERE plan_id=? AND version=? AND publicity_state='scheduled'",
                         (plan_id, version))
            conn.execute("UPDATE publicity_commitments SET status='voided' "
                         "WHERE plan_id=? AND version=? AND status='promised_pending'",
                         (plan_id, version))
            conn.execute("UPDATE plan_versions SET status=? WHERE plan_id=? AND version=?",
                         (PLAN_EXPIRED, plan_id, version))
            self._timeline(conn, plan_id, version, "version_aborted", actor_id,
                           {"reason": self._note(reason, 500)})
            self._audit(conn, actor_id=actor_id, action="plan.aborted",
                        resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                        detail={"reason": self._note(reason, 500)})
            return self._store_receipt(
                conn, request_id=request_id, action="abort_version", payload=payload,
                resource_type="plan_version", resource_id=f"{plan_id}/v{version}",
                response={"plan_id": plan_id, "version": version, "status": PLAN_EXPIRED})

    # ------------------------------------------------------------------
    # 重排
    # ------------------------------------------------------------------
    def rework_plan(self, *, request_id: str, actor_id: str, plan_id: str, reason: str,
                    replacements: list[dict[str, Any]],
                    sessions: dict[str, list[dict[str, Any]]] | None = None,
                    commitments: dict[str, list[dict[str, Any]]] | None = None,
                    change_summary: str = "",
                    lease_minutes: int = DEFAULT_LEASE_MINUTES) -> WriteReceipt:
        """基于最近版本生成仅替换受影响区段的新草案，其余区段携带保留。"""

        plan_id = self._id(plan_id, "plan_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "reason": reason,
                   "replacements": replacements, "change_summary": change_summary}
        if reason not in REWORK_REASONS:
            raise ValidationError("不支持的重排原因")
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR)
            replay = self._replay(conn, request_id=request_id, action="rework_plan", payload=payload)
            if replay:
                return replay
            self._sweep_conn(conn)
            latest = self._latest_version(conn, plan_id)
            base = self._get_version(conn, plan_id, latest)
            if base["status"] not in (PLAN_PUBLISHED, PLAN_REJECTED, PLAN_EXPIRED):
                raise StateError(f"最近版本状态为 {base['status']}，请先完成或作废当前草案")
            artwork_id = conn.execute("SELECT artwork_id FROM plans WHERE plan_id=?",
                                      (plan_id,)).fetchone()["artwork_id"]
            artwork = self._get_artwork(conn, artwork_id)
            components = self._loads(base["components_json"])
            new_version = latest + 1
            self._validate_lease(lease_minutes)

            base_rows = conn.execute(
                "SELECT * FROM segments WHERE plan_id=? AND version=? ORDER BY start_at,segment_id",
                (plan_id, latest)).fetchall()
            by_id = {r["segment_id"]: dict(r) for r in base_rows}
            replacement_map = self._normalize_replacements(replacements, by_id)
            for old_id in replacement_map:
                if by_id[old_id]["status"] == SEG_HANDED_OVER:
                    raise ValidationError(f"区段 {old_id} 已完成交接，不能被重排；请重排其后继区段")

            base_published = base["status"] == PLAN_PUBLISHED

            def fully_confirmed(segment_id: str) -> bool:
                """该旧版本区段上所有职责是否均已确认。"""

                row = conn.execute(
                    "SELECT COUNT(*) AS total,"
                    "SUM(CASE WHEN action_state='confirmed' THEN 1 ELSE 0 END) AS done "
                    "FROM todos WHERE plan_id=? AND version=? AND segment_id=?",
                    (plan_id, latest, segment_id)).fetchone()
                return bool(row["total"]) and row["total"] == row["done"]

            prepared: list[dict[str, Any]] = []
            resources: dict[str, dict] = {}
            carried_ids: set[str] = set()
            # 未受影响区段：
            #  - 已发布底座：全部携带（含已交接事实）；
            #  - 被拒底座：仅已全部确认的区段携带，其余区段重建草案；
            #  - 过期底座：租约已释放，全部重建。
            for row in base_rows:
                old = dict(row)
                if old["segment_id"] in replacement_map:
                    continue
                seg = self._segment_as_prepared(conn, old, resources)
                # 已发布继承来的区段（带 published_at，可能已交接）在任何底座上都继续携带。
                inherited_published = old["published_at"] is not None
                if base_published or inherited_published or \
                        (base["status"] == PLAN_REJECTED
                         and fully_confirmed(old["segment_id"])):
                    seg["carried"] = True
                    carried_ids.add(old["segment_id"])
                prepared.append(seg)

            for old_id, replacement in replacement_map.items():
                chosen_resource, chosen_case = self._choose_fallback(
                    conn, plan_id=plan_id, artwork=artwork, replacement=replacement,
                    old_segment=by_id[old_id], resources=resources,
                    ignore_segments=set(replacement_map) | carried_ids)
                if by_id[old_id]["kind"] == SEG_VENUE:
                    replacement["requirement"].setdefault("case_resource_id", chosen_case)
                segments_in = [{
                    "segment_id": replacement["segment_id"],
                    "kind": by_id[old_id]["kind"],
                    "resource_id": chosen_resource,
                    "start_at": replacement["start_at"],
                    "end_at": replacement["end_at"],
                    "requirement": replacement["requirement"],
                    "alternatives": replacement["alternatives_list"],
                }]
                fresh, fresh_resources = self._prepare_segments(
                    conn, segments_in=segments_in, artwork=artwork)
                resources.update(fresh_resources)
                prepared.extend(fresh)

            prepared.sort(key=lambda s: (s["start_dt"], s["end_dt"], s["segment_id"]))
            self._validate_itinerary(prepared)

            # 被拒/过期底座上未携带的区段重新走草案占用与确认链。
            fresh_ids = {v["segment_id"] for v in replacement_map.values()}
            rebuilt_ids: set[str] = set()
            if not base_published:
                for seg in prepared:
                    if seg.get("carried") or seg["segment_id"] in fresh_ids:
                        continue
                    rebuilt_ids.add(seg["segment_id"])
                    seg["carried"] = False
                    seg["status"] = SEG_PENDING
                    seg.pop("confirmed_at", None)
                    seg.pop("published_at", None)
                    seg.pop("handover_at", None)
                    seg.pop("handover_by", None)
                    seg.pop("handover_ref", None)
                    self._load_resource(conn, seg["resource_id"], resources)
                    seg["holds"] = [(seg["resource_id"], seg["start_at"], seg["end_at"])]
                    if seg["kind"] == SEG_VENUE:
                        seg["holds"].append((seg["requirement"]["case_resource_id"],
                                             seg["start_at"], seg["end_at"]))
                    seg["confirm_roles"] = CONFIRM_ROLES_FOR_SEGMENT[seg["kind"]]

            session_rows, commitment_rows = self._carry_publicity(
                conn, prepared=prepared, replacement_map=replacement_map,
                sessions_in=sessions or {}, commitments_in=commitments or {},
                base_published=base_published, carried_ids=carried_ids,
                rebuilt_ids=rebuilt_ids)
            # 携带区段继续占用其既有物理租约；被替换区段的旧租约在发布时释放。
            ignore_holds = set(replacement_map) | carried_ids
            self._check_resource_availability(conn, prepared, resources,
                                              ignore_segments=ignore_holds)
            self._persist_draft(
                conn, actor=actor, plan_id=plan_id, version=new_version, artwork=artwork,
                components=components, prepared=prepared, session_rows=session_rows,
                commitment_rows=commitment_rows, lease_minutes=lease_minutes,
                base_version=latest, reason=reason,
                change_summary=self._note(change_summary, 500),
                replacement_map=replacement_map)
            # 携带区段的既有物理租约迁移到新版本（发布时统一提交，旧版本超只释放被替换段）。
            if carried_ids:
                placeholders = ",".join("?" * len(carried_ids))
                conn.execute(
                    f"UPDATE resource_holds SET plan_id=?,version=? WHERE segment_id IN ({placeholders})",
                    (plan_id, new_version, *carried_ids))
            # 被拒底座中已携带的确认结论复制到新版本，保持责任链完整且无需重复确认。
            # 序号按新版本的区段位置重新分配，避免与新建待办撞号。
            if not base_published:
                position = {seg["segment_id"]: index for index, seg in enumerate(prepared)}
                for old_todo in conn.execute(
                        "SELECT role,segment_id,segment_kind,decided_by,decided_at "
                        "FROM todos WHERE plan_id=? AND version=? AND action_state='confirmed' "
                        "ORDER BY sequence", (plan_id, latest)).fetchall():
                    if old_todo["segment_id"] not in carried_ids:
                        continue
                    conn.execute(
                        "INSERT INTO todos(todo_id,plan_id,version,role,segment_id,segment_kind,"
                        "action_state,created_at,sequence,decided_by,decided_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, plan_id, new_version, old_todo["role"],
                         old_todo["segment_id"], old_todo["segment_kind"], "confirmed",
                         self._now(),
                         position[old_todo["segment_id"]] * 10 + ROLE_ORDER[old_todo["role"]],
                         old_todo["decided_by"], old_todo["decided_at"]))
            for old_segment_id in set(replacement_map) | rebuilt_ids | carried_ids:
                conn.execute(
                    "UPDATE incurred_costs SET carried_into_version=? "
                    "WHERE plan_id=? AND version=? AND segment_id=? AND carried_into_version IS NULL",
                    (new_version, plan_id, latest, old_segment_id))
            self._audit(conn, actor_id=actor_id, action="plan.rework_created",
                        resource_type="plan_version", resource_id=f"{plan_id}/v{new_version}",
                        detail={"rework_reason": reason,
                                "replaces": sorted(replacement_map)})
            return self._store_receipt(
                conn, request_id=request_id, action="rework_plan", payload=payload,
                resource_type="plan_version", resource_id=f"{plan_id}/v{new_version}",
                response={"plan_id": plan_id, "version": new_version,
                          "replaces": sorted(replacement_map)})

    def _normalize_replacements(self, replacements: list[dict[str, Any]],
                                by_id: dict[str, dict]) -> dict[str, dict[str, Any]]:
        if not isinstance(replacements, list) or not replacements:
            raise ValidationError("replacements 必须是非空列表（重排至少影响一个区段）")
        result: dict[str, dict[str, Any]] = {}
        new_ids: set[str] = set()
        for item in replacements:
            if not isinstance(item, dict):
                raise ValidationError("替换项必须是对象")
            replaces = self._id(item.get("replaces_segment_id", ""), "replaces_segment_id")
            if replaces not in by_id:
                raise ValidationError(f"被替换区段 {replaces} 不存在于最近版本")
            new_seg_id = self._id(item.get("segment_id", ""), "segment_id")
            if new_seg_id in new_ids or new_seg_id in by_id:
                raise ValidationError("新区段编号必须唯一且不能复用旧编号")
            new_ids.add(new_seg_id)
            start = self._dt(item.get("start_at", ""), "start_at")
            end = self._dt(item.get("end_at", ""), "end_at")
            if end <= start:
                raise ValidationError("替换区段结束必须晚于开始")
            requirement = item.get("requirement") or {}
            if not isinstance(requirement, dict):
                raise ValidationError("requirement 必须是对象")
            prefer = item.get("prefer_resources") or []
            alternatives = item.get("alternatives") or []
            if not isinstance(prefer, list) or not isinstance(alternatives, list):
                raise ValidationError("备选资源必须是列表")
            result[replaces] = {
                "segment_id": new_seg_id,
                "start_dt": start, "end_dt": end,
                "start_at": self._ts(start), "end_at": self._ts(end),
                "explicit_resource": item.get("resource_id"),
                "prefer_resources": [self._id(v, "prefer_resources") for v in prefer],
                "alternatives_list": [self._id(v, "alternatives") for v in alternatives],
                "requirement": requirement,
            }
        return result

    def _segment_as_prepared(self, conn, row: dict, resources: dict) -> dict:
        requirement = self._loads(row["requirement_json"])
        holds = [(row["resource_id"], row["start_at"], row["end_at"])]
        if row["kind"] == SEG_VENUE:
            holds.append((requirement["case_resource_id"], row["start_at"], row["end_at"]))
        self._load_resource(conn, row["resource_id"], resources)
        if row["kind"] == SEG_VENUE:
            self._load_resource(conn, requirement["case_resource_id"], resources)
        return {
            "segment_id": row["segment_id"], "kind": row["kind"],
            "start_dt": self._dt(row["start_at"], "start_at"),
            "end_dt": self._dt(row["end_at"], "end_at"),
            "start_at": row["start_at"], "end_at": row["end_at"],
            "resource_id": row["resource_id"], "requirement": requirement,
            "alternatives": tuple(self._loads(row["alternatives_json"])),
            "status": row["status"],
            "confirmed_at": row["confirmed_at"], "published_at": row["published_at"],
            "handover_at": row["handover_at"], "handover_by": row["handover_by"],
            "handover_ref": row["handover_ref"],
            "holds": holds, "confirm_roles": (), "carried": False,
        }

    def _choose_fallback(self, conn, *, plan_id: str, artwork: dict, replacement: dict,
                         old_segment: dict, resources: dict,
                         ignore_segments: set[str]) -> tuple[str, str | None]:
        """按稳定优先级选择资源：显式指定 → prefer → 旧备选 → 同类型稳定排序。

        返回 (主资源ID, 展柜ID或None)。全部不可用时抛出带尝试明细的冲突。
        """

        kind = RESOURCE_KIND_FOR_SEGMENT[old_segment["kind"]]
        start_at, end_at = replacement["start_at"], replacement["end_at"]
        old_req = self._loads(old_segment["requirement_json"])
        requirement = replacement["requirement"]

        candidates: list[str] = []
        if replacement["explicit_resource"]:
            candidates.append(self._id(replacement["explicit_resource"], "resource_id"))
        candidates.extend(replacement["prefer_resources"])
        candidates.extend(self._loads(old_segment["alternatives_json"]))
        candidates.extend(replacement["alternatives_list"])
        for row in conn.execute("SELECT resource_id FROM resources WHERE kind=? ORDER BY created_at,resource_id",
                                (kind,)).fetchall():
            if row["resource_id"] not in candidates:
                candidates.append(row["resource_id"])

        city = requirement.get("_city") or old_req.get("_city")
        currency = requirement.get("currency") or old_req.get("currency")
        tried: list[dict[str, str]] = []
        for candidate_id in candidates:
            row = conn.execute("SELECT * FROM resources WHERE resource_id=?", (candidate_id,)).fetchone()
            if row is None:
                continue
            caps = self._loads(row["capabilities_json"])
            chosen_case: str | None = None
            if old_segment["kind"] == SEG_VENUE:
                if caps.get("city") != city:
                    tried.append({"resource_id": candidate_id, "reason": "city_mismatch"})
                    continue
                if self._dt(caps["window_start"], "window_start") > replacement["start_dt"] or \
                        self._dt(caps["window_end"], "window_end") < replacement["end_dt"]:
                    tried.append({"resource_id": candidate_id, "reason": "window_too_short"})
                    continue
                venue_org = self._owner_org(conn, row["owner_id"])
                qual = conn.execute(
                    "SELECT 1 FROM receiver_qualifications WHERE venue_organization_id=? "
                    "AND artwork_id=? AND qualified=1", (venue_org, artwork["artwork_id"])).fetchone()
                if qual is None:
                    tried.append({"resource_id": candidate_id, "reason": "receiver_not_qualified"})
                    continue
                case_id = requirement.get("case_resource_id")
                if case_id:
                    case = conn.execute("SELECT * FROM resources WHERE resource_id=?",
                                        (case_id,)).fetchone()
                    if case is None or self._loads(case["capabilities_json"]).get("city") != city:
                        tried.append({"resource_id": candidate_id, "reason": "case_unavailable"})
                        continue
                    if self._owner_org(conn, case["owner_id"]) != venue_org:
                        tried.append({"resource_id": candidate_id, "reason": "case_org_mismatch"})
                        continue
                else:
                    case = self._pick_case(conn, venue_org=venue_org, city=city, start_at=start_at,
                                           end_at=end_at, ignore_segments=ignore_segments)
                    if case is None:
                        tried.append({"resource_id": candidate_id, "reason": "case_unavailable"})
                        continue
                    case_id = case
                install = requirement.get("install_hours", old_req.get("install_hours"))
                dismantle = requirement.get("dismantle_hours", old_req.get("dismantle_hours"))
                if install + dismantle > (replacement["end_dt"] - replacement["start_dt"]).total_seconds() / 3600:
                    tried.append({"resource_id": candidate_id, "reason": "window_too_short"})
                    continue
                requirement.setdefault("install_hours", install)
                requirement.setdefault("dismantle_hours", dismantle)
                chosen_case = case_id
            elif old_segment["kind"] == SEG_TRANSPORT:
                requirement.setdefault("from_city", old_req.get("from_city"))
                requirement.setdefault("to_city", old_req.get("to_city"))
                load = requirement.get("load_volume", old_req.get("load_volume"))
                if load is not None and load > caps["capacity"]:
                    tried.append({"resource_id": candidate_id, "reason": "capacity_insufficient"})
                    continue
            else:
                requirement.setdefault("currency", currency)
                amount = requirement.get("coverage_amount", old_req.get("coverage_amount"))
                if caps.get("currency") != currency:
                    tried.append({"resource_id": candidate_id, "reason": "currency_mismatch"})
                    continue
                if amount > caps["capacity"]:
                    tried.append({"resource_id": candidate_id, "reason": "quota_insufficient"})
                    continue
            if self._resource_free(conn, resource_id=candidate_id, start_at=start_at, end_at=end_at,
                                   ignore_segments=ignore_segments,
                                   coverage=requirement.get("coverage_amount")
                                   or old_req.get("coverage_amount")):
                return candidate_id, chosen_case
            tried.append({"resource_id": candidate_id, "reason": "already_booked"})
        err = ConflictError("按备用优先级没有可用资源")
        err.blockers = tried  # type: ignore[attr-defined]
        raise err

    def _pick_case(self, conn, *, venue_org: str, city: str, start_at: str, end_at: str,
                   ignore_segments: set[str]) -> str | None:
        for row in conn.execute(
                "SELECT r.* FROM resources r JOIN participants p ON p.participant_id=r.owner_id "
                "WHERE r.kind='display_case' AND p.organization_id=? "
                "ORDER BY r.created_at,r.resource_id", (venue_org,)):
            caps = self._loads(row["capabilities_json"])
            if caps.get("city") != city:
                continue
            active = self._active_holds(conn, resource_id=row["resource_id"], start_at=start_at,
                                        end_at=end_at, ignore_segments=ignore_segments)
            if not active:
                return row["resource_id"]
        return None

    def _resource_free(self, conn, *, resource_id: str, start_at: str, end_at: str,
                       ignore_segments: set[str], coverage: float | None = None) -> bool:
        active = self._active_holds(conn, resource_id=resource_id, start_at=start_at, end_at=end_at,
                                    ignore_segments=ignore_segments)
        kind = conn.execute("SELECT kind FROM resources WHERE resource_id=?",
                            (resource_id,)).fetchone()["kind"]
        if kind == "insurance_quota" and coverage is not None:
            used = float(coverage) + sum(
                float(self._loads(h["requirement_json"]).get("coverage_amount", 0)) for h in active)
            caps = self._loads(conn.execute(
                "SELECT capabilities_json FROM resources WHERE resource_id=?",
                (resource_id,)).fetchone()["capabilities_json"])
            return used <= float(caps["capacity"]) + 1e-9
        return not active

    def _carry_publicity(self, conn, *, prepared: list[dict], replacement_map: dict[str, dict],
                         sessions_in: dict[str, Any], commitments_in: dict[str, Any],
                         base_published: bool, carried_ids: set[str],
                         rebuilt_ids: set[str]
                         ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """公开场次/宣传承诺的携带规则：

        - 携带区段（已发布，或被拒底座上已确认）沿用原记录，不复制不覆盖；
        - 重建区段（被拒底座上未确认、过期底座全部）按原内容重新生成草案记录；
        - 替换场馆区段必须重新申报。
        """

        session_rows: dict[str, list[dict]] = {}
        commitment_rows: dict[str, list[dict]] = {}
        fresh_ids = {v["segment_id"] for v in replacement_map.values()}
        for seg in prepared:
            if seg["kind"] != SEG_VENUE or seg["segment_id"] in fresh_ids:
                continue
            if seg["segment_id"] in carried_ids:
                continue  # 已公布或已确认：原场次/承诺记录继续有效。
            old_sessions = conn.execute(
                "SELECT starts_at,label,capacity FROM audience_sessions WHERE segment_id=? "
                "ORDER BY starts_at,label", (seg["segment_id"],)).fetchall()
            session_rows[seg["segment_id"]] = [
                {"session_id": uuid.uuid4().hex, "starts_at": r["starts_at"], "label": r["label"],
                 "capacity": r["capacity"]} for r in old_sessions]
            old_commitments = conn.execute(
                "SELECT channel,promised_at,detail FROM publicity_commitments "
                "WHERE venue_segment_id=? ORDER BY promised_at", (seg["segment_id"],)).fetchall()
            commitment_rows[seg["segment_id"]] = [
                {"commitment_id": uuid.uuid4().hex, "channel": r["channel"],
                 "promised_at": r["promised_at"], "detail": r["detail"]}
                for r in old_commitments]
        # 替换的场馆区段按新申报内容校验（运输/保险替换不需要场次与承诺）。
        reverse = {v["segment_id"]: old for old, v in replacement_map.items()}
        fresh_sessions: dict[str, Any] = {}
        fresh_commitments: dict[str, Any] = {}
        fresh_venue_ids = {seg["segment_id"] for seg in prepared
                           if seg["kind"] == SEG_VENUE and seg["segment_id"] in reverse}
        for new_id in fresh_venue_ids:
            old_id = reverse[new_id]
            fresh_sessions[new_id] = sessions_in.get(new_id, sessions_in.get(old_id, []))
            fresh_commitments[new_id] = commitments_in.get(new_id, commitments_in.get(old_id, []))
        fresh_prepared = [seg for seg in prepared if seg["segment_id"] in fresh_venue_ids]
        new_sessions, new_commitments = self._validate_publicity(
            fresh_prepared, fresh_sessions, fresh_commitments)
        session_rows.update(new_sessions)
        commitment_rows.update(new_commitments)
        return session_rows, commitment_rows

    # ------------------------------------------------------------------
    # 交接 / 费用 / 宣传承诺
    # ------------------------------------------------------------------
    def record_handover(self, *, request_id: str, actor_id: str, plan_id: str, segment_id: str,
                        handover_ref: str, note: str = "") -> WriteReceipt:
        plan_id = self._id(plan_id, "plan_id")
        segment_id = self._id(segment_id, "segment_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "segment_id": segment_id,
                   "handover_ref": handover_ref}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            replay = self._replay(conn, request_id=request_id, action="record_handover",
                                  payload=payload)
            if replay:
                return replay
            plan = conn.execute("SELECT published_version FROM plans WHERE plan_id=?",
                                (plan_id,)).fetchone()
            if plan is None or plan["published_version"] is None:
                raise NotFoundError("计划尚未发布")
            version = plan["published_version"]
            seg = conn.execute("SELECT * FROM segments WHERE plan_id=? AND version=? AND segment_id=?",
                               (plan_id, version, segment_id)).fetchone()
            if seg is None:
                raise NotFoundError("当前生效版本上没有该区段")
            if seg["kind"] == SEG_INSURANCE:
                raise ValidationError("保险区段没有实物交接")
            if seg["status"] == SEG_HANDED_OVER:
                raise StateError("该区段已完成交接，记录不可覆盖")
            resource = conn.execute("SELECT owner_id FROM resources WHERE resource_id=?",
                                    (seg["resource_id"],)).fetchone()
            allowed_role = {SEG_VENUE: ROLE_VENUE, SEG_TRANSPORT: ROLE_CARRIER}[seg["kind"]]
            if actor.role not in (ROLE_OPERATOR, allowed_role) or \
                    (actor.role == allowed_role and actor.participant_id != resource["owner_id"]):
                raise PermissionDenied("只有接收方或运营人员可以登记交接")
            handover_ref = self._text(handover_ref, "handover_ref", 120)
            now = self._now()
            conn.execute("UPDATE segments SET status=?,handover_at=?,handover_by=?,handover_ref=? "
                         "WHERE segment_id=?",
                         (SEG_HANDED_OVER, now, actor_id, handover_ref, segment_id))
            self._timeline(conn, plan_id, version, "handover_recorded", actor_id,
                           {"segment_id": segment_id, "handover_ref": handover_ref, "note": note})
            self._audit(conn, actor_id=actor_id, action="segment.handover_recorded",
                        resource_type="segment", resource_id=segment_id,
                        detail={"plan_id": plan_id, "version": version, "handover_ref": handover_ref})
            return self._store_receipt(
                conn, request_id=request_id, action="record_handover", payload=payload,
                resource_type="segment", resource_id=segment_id,
                response={"segment_id": segment_id, "status": SEG_HANDED_OVER})

    def record_cost(self, *, request_id: str, actor_id: str, plan_id: str, segment_id: str,
                    amount: str, currency: str, category: str, note: str = "") -> WriteReceipt:
        plan_id = self._id(plan_id, "plan_id")
        segment_id = self._id(segment_id, "segment_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "segment_id": segment_id,
                   "amount": str(amount), "currency": currency, "category": category}
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            replay = self._replay(conn, request_id=request_id, action="record_cost", payload=payload)
            if replay:
                return replay
            if conn.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone() is None:
                raise NotFoundError("计划不存在")
            target_version = conn.execute(
                "SELECT COALESCE(published_version,current_version) AS v FROM plans WHERE plan_id=?",
                (plan_id,)).fetchone()["v"]
            seg = conn.execute(
                "SELECT version,resource_id FROM segments WHERE plan_id=? AND version=? AND segment_id=?",
                (plan_id, target_version, segment_id)).fetchone()
            if seg is None:
                raise NotFoundError("当前版本上没有该区段")
            owner = conn.execute("SELECT owner_id FROM resources WHERE resource_id=?",
                                 (seg["resource_id"],)).fetchone()
            if actor.role != ROLE_OPERATOR and actor.participant_id != owner["owner_id"]:
                raise PermissionDenied("只有运营人员或资源所有方可以登记费用")
            try:
                decimal_amount = f"{float(amount):.2f}"
                if float(amount) <= 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValidationError("amount 必须是正数") from exc
            currency = self._text(currency, "currency", 8)
            category = self._text(category, "category", 80)
            cost_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO incurred_costs(cost_id,plan_id,version,segment_id,amount,currency,"
                "category,occurred_at,recorded_by,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cost_id, plan_id, seg["version"], segment_id, decimal_amount, currency,
                 category, self._now(), actor_id, self._note(note, 300)))
            self._timeline(conn, plan_id, seg["version"], "cost_recorded", actor_id,
                           {"segment_id": segment_id, "cost_id": cost_id,
                            "amount": decimal_amount, "currency": currency, "category": category})
            self._audit(conn, actor_id=actor_id, action="cost.recorded",
                        resource_type="incurred_cost", resource_id=cost_id,
                        detail={"plan_id": plan_id, "segment_id": segment_id,
                                "amount": decimal_amount, "currency": currency})
            return self._store_receipt(
                conn, request_id=request_id, action="record_cost", payload=payload,
                resource_type="incurred_cost", resource_id=cost_id,
                response={"cost_id": cost_id})

    def resolve_publicity(self, *, request_id: str, actor_id: str, commitment_id: str,
                          outcome: str) -> WriteReceipt:
        commitment_id = self._id(commitment_id, "commitment_id")
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "outcome": outcome}
        if outcome not in ("fulfilled", "broken"):
            raise ValidationError("outcome 必须是 fulfilled 或 broken")
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            replay = self._replay(conn, request_id=request_id, action="resolve_publicity",
                                  payload=payload)
            if replay:
                return replay
            row = conn.execute("SELECT * FROM publicity_commitments WHERE commitment_id=?",
                               (commitment_id,)).fetchone()
            if row is None:
                raise NotFoundError("宣传承诺不存在")
            if actor.role not in (ROLE_OPERATOR, ROLE_VENUE):
                raise PermissionDenied("只有运营人员或场馆方可以更新宣传承诺")
            if row["status"] != "promised":
                raise StateError("承诺状态不允许更新")
            conn.execute("UPDATE publicity_commitments SET status=? WHERE commitment_id=?",
                         (outcome, commitment_id))
            self._timeline(conn, row["plan_id"], row["version"], "publicity_resolved", actor_id,
                           {"commitment_id": commitment_id, "outcome": outcome})
            self._audit(conn, actor_id=actor_id, action="publicity.resolved",
                        resource_type="publicity_commitment", resource_id=commitment_id,
                        detail={"outcome": outcome})
            return self._store_receipt(
                conn, request_id=request_id, action="resolve_publicity", payload=payload,
                resource_type="publicity_commitment", resource_id=commitment_id,
                response={"commitment_id": commitment_id, "status": outcome})

    # ------------------------------------------------------------------
    # 查询：待办 / 计划 / 冲突恢复 / 场次影响 / 时点还原
    # ------------------------------------------------------------------
    def list_todos(self, *, actor_id: str) -> list[dict[str, Any]]:
        """返回参与方自己的待办与最小必要资料（不泄露其他区段详情）。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._sweep_conn(conn)
            rows = conn.execute(
                "SELECT t.todo_id,t.plan_id,t.version,t.role,t.segment_id,t.segment_kind,t.sequence,"
                "s.start_at AS seg_start,s.end_at AS seg_end,s.resource_id AS seg_resource,"
                "s.requirement_json,pv.lease_expires_at,"
                "p.artwork_id,a.representative_id "
                "FROM todos t JOIN segments s ON s.segment_id=t.segment_id AND s.version=t.version "
                "AND s.plan_id=t.plan_id "
                "JOIN plan_versions pv ON pv.plan_id=t.plan_id AND pv.version=t.version "
                "JOIN plans p ON p.plan_id=t.plan_id JOIN artworks a ON a.artwork_id=p.artwork_id "
                "WHERE t.action_state='pending' AND pv.status=? "
                "ORDER BY t.plan_id,t.version,t.sequence", (PLAN_DRAFT,)).fetchall()
            todos = []
            for row in rows:
                if actor.role != ROLE_OPERATOR and not self._todo_visible(conn, row, actor):
                    continue
                todos.append({
                    "todo_id": row["todo_id"], "plan_id": row["plan_id"], "version": row["version"],
                    "role": row["role"], "segment_id": row["segment_id"],
                    "segment_kind": row["segment_kind"], "action": "confirm_or_reject",
                    "segment_start": row["seg_start"], "segment_end": row["seg_end"],
                    "expires_at": row["lease_expires_at"],
                    "minimal": self._minimal_brief(conn, row, actor),
                })
            return todos

    def _todo_visible(self, conn, row, actor: Participant) -> bool:
        if actor.role == ROLE_REPRESENTATIVE:
            return row["role"] == ROLE_REPRESENTATIVE and \
                row["representative_id"] == actor.participant_id
        if actor.role != row["role"]:
            return False
        owner = conn.execute(
            "SELECT p.organization_id FROM resources r JOIN participants p "
            "ON p.participant_id=r.owner_id WHERE r.resource_id=?",
            (row["seg_resource"],)).fetchone()
        return bool(owner and owner["organization_id"] == actor.organization_id)

    def _minimal_brief(self, conn, row, actor: Participant) -> dict[str, Any]:
        requirement = self._loads(row["requirement_json"])
        resource = conn.execute("SELECT * FROM resources WHERE resource_id=?",
                                (row["seg_resource"],)).fetchone()
        caps = self._loads(resource["capabilities_json"])
        artwork = conn.execute("SELECT artwork_id,title,components_json,condition_note FROM artworks "
                               "WHERE artwork_id=?", (row["artwork_id"],)).fetchone()
        brief: dict[str, Any] = {"artwork": {"artwork_id": artwork["artwork_id"],
                                             "title": artwork["title"],
                                             "condition_note": artwork["condition_note"]}}
        if actor.role == ROLE_OPERATOR:
            brief["artwork"]["components"] = self._loads(artwork["components_json"])
            brief["requirement"] = requirement
            brief["resource"] = {"resource_id": row["seg_resource"], "label": resource["label"],
                                 "capabilities": caps}
            return brief
        if actor.role == ROLE_REPRESENTATIVE:
            # 代表核对组件与全程顺序：只给城市/时间/对手方资源标签，不含商务额度。
            brief["artwork"]["components"] = self._loads(artwork["components_json"])
            itinerary = []
            for s in conn.execute(
                    "SELECT s.kind,s.start_at,s.end_at,r.label FROM segments s "
                    "JOIN resources r ON r.resource_id=s.resource_id "
                    "WHERE s.plan_id=? AND s.version=? ORDER BY s.start_at,s.segment_id",
                    (row["plan_id"], row["version"])).fetchall():
                item = {"kind": s["kind"], "start_at": s["start_at"], "end_at": s["end_at"],
                        "counterparty_resource": s["label"]}
                itinerary.append(item)
            brief["itinerary"] = itinerary
            return brief
        brief["artwork"]["components"] = self._loads(artwork["components_json"])
        if row["segment_kind"] == SEG_VENUE:
            brief["venue"] = {"resource_id": row["seg_resource"], "label": resource["label"],
                              "city": caps.get("city"),
                              "window": [caps.get("window_start"), caps.get("window_end")],
                              "certificate": requirement.get("_certificate", "")}
            brief["install_hours"] = requirement.get("install_hours")
            brief["dismantle_hours"] = requirement.get("dismantle_hours")
            brief["sessions"] = [dict(r) for r in conn.execute(
                "SELECT starts_at,label,capacity FROM audience_sessions WHERE segment_id=? "
                "ORDER BY starts_at", (row["segment_id"],))]
            brief["publicity_commitments"] = [dict(r) for r in conn.execute(
                "SELECT channel,promised_at,detail FROM publicity_commitments WHERE venue_segment_id=?",
                (row["segment_id"],))]
        elif row["segment_kind"] == SEG_TRANSPORT:
            brief["vehicle"] = {"resource_id": row["seg_resource"], "label": resource["label"],
                                "capacity": caps.get("capacity")}
            brief["from_city"] = requirement.get("from_city")
            brief["to_city"] = requirement.get("to_city")
            brief["load_volume"] = requirement.get("load_volume")
        else:
            brief["insurance"] = {"resource_id": row["seg_resource"], "label": resource["label"]}
            brief["coverage_amount"] = requirement.get("coverage_amount")
            brief["currency"] = requirement.get("currency")
        return brief

    def get_plan(self, *, actor_id: str, plan_id: str) -> dict[str, Any]:
        plan_id = self._id(plan_id, "plan_id")
        with self.database.transaction() as conn:
            self._participant(conn, actor_id)
            plan = conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("计划不存在")
            versions = []
            for vrow in conn.execute("SELECT * FROM plan_versions WHERE plan_id=? ORDER BY version",
                                     (plan_id,)).fetchall():
                version = dict(vrow)
                segments = []
                for srow in conn.execute(
                        "SELECT * FROM segments WHERE plan_id=? AND version=? ORDER BY start_at,segment_id",
                        (plan_id, version["version"])).fetchall():
                    seg = dict(srow)
                    seg["requirement"] = self._loads(seg.pop("requirement_json"))
                    seg["alternatives"] = self._loads(seg.pop("alternatives_json"))
                    # 携带到后继版本的场馆区段沿用原版本的公开场次，不复制不覆盖。
                    seg["sessions"] = [dict(r) for r in conn.execute(
                        "SELECT session_id,starts_at,label,capacity,publicity_state FROM audience_sessions "
                        "WHERE segment_id=? ORDER BY starts_at", (srow["segment_id"],))]
                    seg["costs"] = [dict(r) for r in conn.execute(
                        "SELECT cost_id,amount,currency,category,occurred_at,carried_into_version "
                        "FROM incurred_costs WHERE plan_id=? AND segment_id=? "
                        "ORDER BY occurred_at,cost_id",
                        (plan_id, srow["segment_id"]))]
                    segments.append(seg)
                version["segments"] = segments
                version["todos"] = [dict(r) for r in conn.execute(
                    "SELECT role,segment_id,action_state,decided_by,decided_at,sequence "
                    "FROM todos WHERE plan_id=? AND version=? ORDER BY sequence",
                    (plan_id, version["version"]))]
                version["timeline"] = [
                    {"sequence": r["sequence"], "event_type": r["event_type"], "actor_id": r["actor_id"],
                     "at": r["at"], "detail": self._loads(r["detail_json"])}
                    for r in conn.execute(
                        "SELECT * FROM version_timeline WHERE plan_id=? AND version=? ORDER BY sequence",
                        (plan_id, version["version"]))]
                version["components"] = self._loads(version.pop("components_json"))
                versions.append(version)
            return {"plan_id": plan_id, "artwork_id": plan["artwork_id"],
                    "current_version": plan["current_version"],
                    "published_version": plan["published_version"], "versions": versions}

    def _availability_conn(self, conn, *, kind: str, start_at: str, end_at: str,
                           city: str | None, coverage: float | None) -> dict[str, Any]:
        items = []
        for row in conn.execute("SELECT * FROM resources WHERE kind=? ORDER BY created_at,resource_id",
                                (kind,)).fetchall():
            caps = self._loads(row["capabilities_json"])
            if city and caps.get("city") != city:
                continue
            holds = self._active_holds(conn, resource_id=row["resource_id"], start_at=start_at,
                                       end_at=end_at, ignore_segments=set())
            detail: dict[str, Any] = {"resource_id": row["resource_id"], "label": row["label"],
                                      "owner_id": row["owner_id"], "capabilities": caps}
            if kind == "insurance_quota":
                used = float(coverage or 0) + sum(
                    float(self._loads(h["requirement_json"]).get("coverage_amount", 0)) for h in holds)
                detail["projected_used"] = used
                detail["available"] = used <= float(caps["capacity"]) + 1e-9
            else:
                detail["available"] = not holds
            detail["blocked_by"] = [{"segment_id": h["segment_id"], "plan_id": h["plan_id"],
                                     "version": h["version"]} for h in holds]
            items.append(detail)
        return {"kind": kind, "start_at": start_at, "end_at": end_at, "items": items}

    def availability(self, *, kind: str, start_at: str, end_at: str, city: str | None = None,
                     coverage: float | None = None) -> dict[str, Any]:
        """列出某时段资源是否可用及占用来源，供冲突解释与恢复路径选择。"""

        if kind not in RESOURCE_OWNER_ROLE:
            raise ValidationError("kind 不支持")
        start = self._dt(start_at, "start_at")
        end = self._dt(end_at, "end_at")
        if end <= start:
            raise ValidationError("结束必须晚于开始")
        with self.database.transaction() as conn:
            return self._availability_conn(conn, kind=kind, start_at=self._ts(start),
                                           end_at=self._ts(end), city=city, coverage=coverage)

    def recovery_options(self, *, actor_id: str, plan_id: str) -> dict[str, Any]:
        """给出冲突/拒绝原因、稳定优先级的恢复候选和下一步动作。"""

        plan_id = self._id(plan_id, "plan_id")
        with self.database.transaction(immediate=True) as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR, ROLE_AUDITOR)
            self._sweep_conn(conn)
            latest = self._latest_version(conn, plan_id)
            ver = self._get_version(conn, plan_id, latest)
            reasons: list[dict[str, Any]] = []
            next_actions: list[str] = []
            if ver["status"] == PLAN_REJECTED:
                for r in conn.execute(
                        "SELECT t.segment_id,t.role,t.decided_by,t.decided_at,pv.reject_reason "
                        "FROM todos t JOIN plan_versions pv ON pv.plan_id=t.plan_id AND pv.version=t.version "
                        "WHERE t.plan_id=? AND t.version=? AND t.action_state='rejected'",
                        (plan_id, latest)).fetchall():
                    reasons.append({"type": "rejection", **dict(r)})
                next_actions.append("调用 rework_plan 替换被拒区段，未受影响区段会自动携带")
            elif ver["status"] == PLAN_EXPIRED:
                reasons.append({"type": "lease_expired",
                                "lease_expires_at": ver["lease_expires_at"]})
                next_actions.append("草案租约已释放，可用 rework_plan 重新提交")
            elif ver["status"] == PLAN_DRAFT:
                pending = conn.execute(
                    "SELECT role,segment_id FROM todos WHERE plan_id=? AND version=? "
                    "AND action_state='pending' ORDER BY sequence", (plan_id, latest)).fetchall()
                reasons.append({"type": "awaiting_confirmation",
                                "pending": [dict(r) for r in pending]})
                next_actions.append("等待剩余参与方确认后调用 publish")
            else:
                next_actions.append("版本已生效；如需改期调用 rework_plan")
            options: dict[str, Any] = {}
            for seg in conn.execute(
                    "SELECT * FROM segments WHERE plan_id=? AND version=? ORDER BY start_at,segment_id",
                    (plan_id, latest)).fetchall():
                kind = RESOURCE_KIND_FOR_SEGMENT[seg["kind"]]
                city = None
                coverage = None
                if seg["kind"] == SEG_VENUE:
                    city = self._loads(seg["requirement_json"]).get("_city")
                if seg["kind"] == SEG_INSURANCE:
                    coverage = self._loads(seg["requirement_json"]).get("coverage_amount")
                avail = self._availability_conn(conn, kind=kind, start_at=seg["start_at"],
                                                end_at=seg["end_at"], city=city, coverage=coverage)
                options[seg["segment_id"]] = [
                    {"resource_id": item["resource_id"], "label": item["label"],
                     "available": item["available"], "blocked_by": item["blocked_by"]}
                    for item in avail["items"]]
            return {"plan_id": plan_id, "version": latest, "status": ver["status"],
                    "reasons": reasons, "options": options, "next_actions": next_actions}

    def sessions_impact(self, *, actor_id: str, plan_id: str) -> dict[str, Any]:
        """列出每次改期影响的观众场次（shifted/cancelled），公开排期不被静默覆盖。"""

        plan_id = self._id(plan_id, "plan_id")
        with self.database.transaction() as conn:
            self._participant(conn, actor_id)
            sessions = [dict(r) for r in conn.execute(
                "SELECT session_id,version,segment_id,starts_at,label,capacity,publicity_state "
                "FROM audience_sessions WHERE plan_id=? AND publicity_state IN ('shifted','cancelled') "
                "ORDER BY version,starts_at", (plan_id,))]
            events = []
            for trow in conn.execute(
                    "SELECT version,event_type,actor_id,at,detail_json FROM version_timeline "
                    "WHERE plan_id=? ORDER BY version,sequence", (plan_id,)):
                detail = self._loads(trow["detail_json"])
                if detail.get("impacted_sessions"):
                    events.append({"version": trow["version"], "event_type": trow["event_type"],
                                   "actor_id": trow["actor_id"], "at": trow["at"],
                                   "impacted_sessions": detail["impacted_sessions"]})
            return {"plan_id": plan_id, "sessions": sessions, "events": events}

    def snapshot_at(self, *, actor_id: str, plan_id: str, at: str) -> dict[str, Any]:
        """审计员按历史时点还原版本状态、责任链、在途交接与费用。"""

        plan_id = self._id(plan_id, "plan_id")
        point = self._ts(self._dt(at, "at"))
        with self.database.transaction() as conn:
            actor = self._participant(conn, actor_id)
            self._require(actor, ROLE_OPERATOR, ROLE_AUDITOR)
            if conn.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone() is None:
                raise NotFoundError("计划不存在")
            versions = []
            vrows = conn.execute(
                "SELECT version FROM plan_versions WHERE plan_id=? AND created_at<=? ORDER BY version",
                (plan_id, point)).fetchall()
            for vrow in vrows:
                version = vrow["version"]
                status = "nonexistent"
                published_at = None
                handed: list[str] = []
                confirmed: set[str] = set()
                rejected_at = None
                expired = False
                timeline = []
                for trow in conn.execute(
                        "SELECT sequence,event_type,actor_id,at,detail_json FROM version_timeline "
                        "WHERE plan_id=? AND version=? AND at<=? ORDER BY sequence",
                        (plan_id, version, point)).fetchall():
                    detail = self._loads(trow["detail_json"])
                    timeline.append({"sequence": trow["sequence"], "event_type": trow["event_type"],
                                     "actor_id": trow["actor_id"], "at": trow["at"], "detail": detail})
                    etype = trow["event_type"]
                    if etype == "version_created":
                        status = PLAN_DRAFT
                    elif etype == "segment_rejected":
                        status, rejected_at = PLAN_REJECTED, trow["at"]
                    elif etype == "version_published":
                        status, published_at = PLAN_PUBLISHED, trow["at"]
                    elif etype == "version_superseded":
                        status = PLAN_SUPERSEDED
                    elif etype == "version_expired":
                        status, expired = PLAN_EXPIRED, True
                    elif etype == "segment_confirmed":
                        confirmed.add(f"{detail['role']}:{detail['segment_id']}")
                    elif etype == "handover_recorded":
                        handed.append(detail["segment_id"])
                pending = []
                if status == PLAN_DRAFT:
                    for todo in conn.execute(
                            "SELECT role,segment_id FROM todos WHERE plan_id=? AND version=? "
                            "AND action_state NOT IN ('voided') ORDER BY sequence",
                            (plan_id, version)).fetchall():
                        if f"{todo['role']}:{todo['segment_id']}" not in confirmed:
                            pending.append(f"{todo['role']}:{todo['segment_id']}")
                costs = [dict(r) for r in conn.execute(
                    "SELECT cost_id,segment_id,amount,currency,category,occurred_at "
                    "FROM incurred_costs WHERE plan_id=? AND version=? AND occurred_at<=? "
                    "ORDER BY occurred_at,cost_id", (plan_id, version, point))]
                versions.append({"version": version, "status_as_of": status,
                                 "published_at_as_of": published_at,
                                 "rejected_at_as_of": rejected_at, "expired": expired,
                                 "pending_todos": pending, "handed_over_segments": handed,
                                 "costs_on_record": costs, "timeline": timeline})
            return {"plan_id": plan_id, "as_of": point, "versions": versions}

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)
        ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"], "actor_id": row["actor_id"],
                 "action": row["action"], "resource_type": row["resource_type"],
                 "resource_id": row["resource_id"], "detail": self._loads(row["detail_json"]),
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "occurred_at": row["occurred_at"]} for row in rows]

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)
