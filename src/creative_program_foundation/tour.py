"""巡展与渠道调度领域服务。

把作品及组件、场馆窗口、展柜条件、布撤展工时、运输区段、承运能力、
保险额度、接收资质与宣传承诺纳入同一套计划版本：

- 限时草案以租约形式占用渠道资源，到期自动失效；
- 作品代表、保险经办、场馆、承运方按固定顺序确认后才能发布；
- 拒签、窗口缩短、运输延误、作品损伤、临时闭馆只重排受影响区段，
  已完成交接、已发生费用、公开排期不会被静默覆盖；
- 备用场馆与承运方案按清单中的稳定优先级启用；
- 发布互斥，同一计划并发发布最多一个版本生效。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .storage import Database

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_REP = "reviewer"
ROLE_AUDITOR = "auditor"

LEASE_DEFAULT_SECONDS = 300

# 版本状态
DRAFT = "draft"
BLOCKED = "blocked"
REJECTED = "rejected"
PUBLISHED = "published"
SUPERSEDED = "superseded"

# 资源租约状态
HELD = "held"
CONFIRMED = "confirmed"
RELEASED = "released"
EXPIRED = "expired"
BLOCKED_ALLOC = "blocked"

ACTIVE_ALLOC = (HELD, CONFIRMED)

PARTY_REPRESENTATIVE = "representative"
PARTY_INSURANCE = "insurance"
PARTY_VENUE = "venue"
PARTY_CARRIER = "carrier"


def parse_ts(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须携带时区")
    return parsed.astimezone(timezone.utc)


def fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_DATETIME_KEYS = (
    "install_at_dt", "open_at_dt", "close_at_dt", "dismantle_at_dt",
    "starts_at_dt", "ends_at_dt", "pickup_at_dt", "deliver_at_dt",
    "cover_from_dt", "cover_to_dt",
)


def strip_datetimes(value: Any) -> Any:
    """删除仅用于进程内比较的 datetime 字段，使清单可被 JSON 持久化。"""

    if isinstance(value, dict):
        return {k: strip_datetimes(v) for k, v in value.items() if k not in _DATETIME_KEYS}
    if isinstance(value, list):
        return [strip_datetimes(v) for v in value]
    return value


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} 必须是对象")
    return value


def _require_str(value: Any, field: str, limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 不能为空")
    value = value.strip()
    if len(value) > limit:
        raise ValidationError(f"{field} 不能超过 {limit} 个字符")
    return value


def _require_number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是数字")
    value = float(value)
    if value < minimum:
        raise ValidationError(f"{field} 不能小于 {minimum}")
    return value


class TourService:
    """实现巡展计划的登记、占用、确认、发布与重排。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_str(self) -> str:
        return fmt(self._now())

    def _actor(self, conn, actor_id: str) -> Actor:
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create) -> tuple[str, str, dict[str, Any], bool]:
        request_id = _require_str(request_id, "request_id", 80)
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = __import__("json").loads(row["response_json"])
            return row["resource_type"], row["resource_id"], response, True
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_str()),
        )
        return resource_type, resource_id, response, False

    def reconcile(self) -> int:
        """服务启动或恢复时调用：清理到期租约，返回被协调的失效草案数。"""

        with self.database.transaction(immediate=True) as conn:
            return self.sweep_expired(conn)

    def sweep_expired(self, conn) -> int:
        """把到期的草案租约标记失效，返回失效版本数。重启后调用同样安全。"""

        now = self._now_str()
        expired = conn.execute(
            "SELECT plan_id, version FROM tour_plans "
            "WHERE status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
            (DRAFT, now),
        ).fetchall()
        for row in expired:
            self._expire_version(conn, row["plan_id"], row["version"], "租约到期未完成确认")
        return len(expired)

    def _expire_version(self, conn, plan_id: str, version: int, reason: str) -> None:
        conn.execute(
            "UPDATE tour_plans SET status=?, lease_expires_at=NULL WHERE plan_id=? AND version=? AND status=?",
            (BLOCKED, plan_id, version, DRAFT),
        )
        conn.execute(
            "UPDATE resource_allocations SET status=?, lease_expires_at=NULL, released_at=? "
            "WHERE plan_id=? AND version=? AND status=?",
            (EXPIRED, self._now_str(), plan_id, version, HELD),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_conflicts(plan_id,version,detail_json,created_at) VALUES(?,?,?,?)",
            (plan_id, version, canonical_json({"kind": "lease_expired", "reason": reason,
                                               "recovery": ["重新生成草案并按优先级选择资源"]}),
             self._now_str()),
        )
        append_event(conn, actor_id="system", action="tour.lease_expired",
                     resource_type="tour_plan", resource_id=plan_id,
                     detail={"version": version, "reason": reason}, occurred_at=self._now_str())

    # ------------------------------------------------------------ 渠道资源登记

    def register_artwork(self, *, request_id: str, actor_id: str, artwork_id: str, title: str,
                         representative_id: str, declared_value: float,
                         spec: dict[str, Any]) -> dict[str, Any]:
        payload = {"artwork_id": artwork_id, "title": title, "representative_id": representative_id,
                   "declared_value": declared_value, "spec": spec}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以登记作品")
            artwork_id = _require_str(artwork_id, "artwork_id")
            title = _require_str(title, "title")
            rep = self._actor(conn, representative_id)
            if rep.role not in (ROLE_REP, ROLE_ADMIN):
                raise ValidationError("作品代表必须具备代表角色")
            value = _require_number(declared_value, "declared_value")
            spec = self._clean_artwork_spec(spec)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO artworks(artwork_id,title,representative_id,declared_value,spec_json,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (artwork_id, title, representative_id, value, canonical_json(spec), self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已存在") from exc
                append_event(conn, actor_id=actor_id, action="artwork.registered",
                             resource_type="artwork", resource_id=artwork_id,
                             detail={"title": title, "representative_id": representative_id,
                                     "declared_value": value}, occurred_at=self._now_str())
                return "artwork", artwork_id, {"artwork_id": artwork_id}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="register_artwork", payload=payload, create=create)
            return {**response, "replayed": replayed}

    @staticmethod
    def _clean_artwork_spec(spec: Any) -> dict[str, Any]:
        spec = _require_dict(spec, "spec")
        cleaned = {
            "pieces": int(_require_number(spec.get("pieces", 1), "spec.pieces", minimum=1)),
            "weight_kg": _require_number(spec.get("weight_kg", 0), "spec.weight_kg"),
            "volume_m3": _require_number(spec.get("volume_m3", 0), "spec.volume_m3"),
            "install_hours": _require_number(spec.get("install_hours", 0), "spec.install_hours"),
            "climate": bool(spec.get("climate", False)),
            "security": bool(spec.get("security", False)),
        }
        return cleaned

    def register_venue(self, *, request_id: str, actor_id: str, venue_id: str, site_id: str,
                       name: str, contact_actor_id: str, required_qualification: str) -> dict[str, Any]:
        payload = {"venue_id": venue_id, "site_id": site_id, "name": name,
                   "contact_actor_id": contact_actor_id,
                   "required_qualification": required_qualification}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以登记场馆")
            venue_id = _require_str(venue_id, "venue_id")
            name = _require_str(name, "name")
            qualification = _require_str(required_qualification, "required_qualification", 80)
            if conn.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            self._actor(conn, contact_actor_id)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO venues(venue_id,site_id,name,contact_actor_id,required_qualification,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (venue_id, site_id, name, contact_actor_id, qualification, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("场馆编号已存在或场所无效") from exc
                append_event(conn, actor_id=actor_id, action="venue.registered",
                             resource_type="venue", resource_id=venue_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now_str())
                return "venue", venue_id, {"venue_id": venue_id}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="register_venue", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _register_child(self, *, request_id: str, actor_id: str, action: str, table: str,
                        resource_type: str, identifier_field: str, identifier: str,
                        parent_check: str, parent_params: tuple, columns: str,
                        params: tuple, audit_detail: dict[str, Any],
                        allowed_roles=(ROLE_ADMIN, ROLE_OPERATOR)) -> dict[str, Any]:
        payload = {"identifier": identifier, "parent": parent_params, "params": params}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in allowed_roles:
                raise PermissionDenied("当前角色不能登记该资源")
            if conn.execute(parent_check, parent_params).fetchone() is None:
                raise NotFoundError("引用的上游资源不存在")
            identifier = _require_str(identifier, identifier_field)

            def create():
                try:
                    conn.execute(f"INSERT INTO {table}({columns}) VALUES({','.join('?' * len(params))})", params)
                except Exception as exc:
                    raise ConflictError(f"{resource_type} 编号已存在或上游无效") from exc
                append_event(conn, actor_id=actor_id, action=f"{resource_type}.registered",
                             resource_type=resource_type, resource_id=identifier,
                             detail=audit_detail, occurred_at=self._now_str())
                return resource_type, identifier, {f"{resource_type}_id": identifier}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action=action, payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_venue_window(self, *, request_id: str, actor_id: str, window_id: str, venue_id: str,
                              starts_at: str, ends_at: str) -> dict[str, Any]:
        start = parse_ts(starts_at, "starts_at")
        end = parse_ts(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("窗口结束时间必须晚于开始时间")
        now = self._now_str()
        return self._register_child(
            request_id=request_id, actor_id=actor_id, action="register_venue_window",
            table="venue_windows", resource_type="venue_window",
            identifier_field="window_id", identifier=window_id,
            parent_check="SELECT 1 FROM venues WHERE venue_id=?", parent_params=(venue_id,),
            columns="window_id,venue_id,starts_at,ends_at,status,version,updated_at",
            params=(window_id, venue_id, fmt(start), fmt(end), "open", 1, now),
            audit_detail={"venue_id": venue_id, "starts_at": fmt(start), "ends_at": fmt(end)})

    def register_display_case(self, *, request_id: str, actor_id: str, case_id: str, venue_id: str,
                              label: str, conditions: dict[str, Any]) -> dict[str, Any]:
        conditions = _require_dict(conditions, "conditions")
        cleaned = {
            "max_weight_kg": _require_number(conditions.get("max_weight_kg", 0), "conditions.max_weight_kg"),
            "max_volume_m3": _require_number(conditions.get("max_volume_m3", 0), "conditions.max_volume_m3"),
            "climate": bool(conditions.get("climate", False)),
            "security": bool(conditions.get("security", False)),
        }
        now = self._now_str()
        return self._register_child(
            request_id=request_id, actor_id=actor_id, action="register_display_case",
            table="display_cases", resource_type="display_case",
            identifier_field="case_id", identifier=case_id,
            parent_check="SELECT 1 FROM venues WHERE venue_id=?", parent_params=(venue_id,),
            columns="case_id,venue_id,label,conditions_json,created_at",
            params=(case_id, venue_id, _require_str(label, "label", 120), canonical_json(cleaned), now),
            audit_detail={"venue_id": venue_id, "label": label})

    def register_labor(self, *, request_id: str, actor_id: str, labor_id: str, venue_id: str,
                       starts_at: str, ends_at: str, available_hours: float) -> dict[str, Any]:
        start = parse_ts(starts_at, "starts_at")
        end = parse_ts(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("工时区段结束时间必须晚于开始时间")
        hours = _require_number(available_hours, "available_hours", minimum=0.1)
        now = self._now_str()
        return self._register_child(
            request_id=request_id, actor_id=actor_id, action="register_labor",
            table="venue_labor_slots", resource_type="venue_labor",
            identifier_field="labor_id", identifier=labor_id,
            parent_check="SELECT 1 FROM venues WHERE venue_id=?", parent_params=(venue_id,),
            columns="labor_id,venue_id,starts_at,ends_at,available_hours,created_at",
            params=(labor_id, venue_id, fmt(start), fmt(end), hours, now),
            audit_detail={"venue_id": venue_id, "available_hours": hours})

    def register_receiver(self, *, request_id: str, actor_id: str, receiver_id: str, venue_id: str,
                          receiver_actor_id: str, qualification: str) -> dict[str, Any]:
        qualification = _require_str(qualification, "qualification", 80)
        now = self._now_str()
        return self._register_child(
            request_id=request_id, actor_id=actor_id, action="register_receiver",
            table="receivers", resource_type="receiver",
            identifier_field="receiver_id", identifier=receiver_id,
            parent_check="SELECT 1 FROM venues v WHERE v.venue_id=?", parent_params=(venue_id,),
            columns="receiver_id,venue_id,actor_id,qualification,active,created_at",
            params=(receiver_id, venue_id, _require_str(receiver_actor_id, "receiver_actor_id"),
                    qualification, 1, now),
            audit_detail={"venue_id": venue_id, "qualification": qualification})

    def register_carrier(self, *, request_id: str, actor_id: str, carrier_id: str, name: str,
                         contact_actor_id: str) -> dict[str, Any]:
        payload = {"carrier_id": carrier_id, "name": name, "contact_actor_id": contact_actor_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以登记承运方")
            carrier_id = _require_str(carrier_id, "carrier_id")
            name = _require_str(name, "name")
            self._actor(conn, contact_actor_id)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO carriers(carrier_id,name,contact_actor_id,created_at) VALUES(?,?,?,?)",
                        (carrier_id, name, contact_actor_id, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("承运方编号已存在") from exc
                append_event(conn, actor_id=actor_id, action="carrier.registered",
                             resource_type="carrier", resource_id=carrier_id,
                             detail={"name": name}, occurred_at=self._now_str())
                return "carrier", carrier_id, {"carrier_id": carrier_id}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="register_carrier", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_carrier_slot(self, *, request_id: str, actor_id: str, slot_id: str, carrier_id: str,
                              starts_at: str, ends_at: str, capacity: dict[str, Any]) -> dict[str, Any]:
        start = parse_ts(starts_at, "starts_at")
        end = parse_ts(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("承运区段结束时间必须晚于开始时间")
        capacity = _require_dict(capacity, "capacity")
        cleaned = {
            "max_weight_kg": _require_number(capacity.get("max_weight_kg", 0), "capacity.max_weight_kg"),
            "max_volume_m3": _require_number(capacity.get("max_volume_m3", 0), "capacity.max_volume_m3"),
            "max_pieces": int(_require_number(capacity.get("max_pieces", 1), "capacity.max_pieces", minimum=1)),
        }
        now = self._now_str()
        return self._register_child(
            request_id=request_id, actor_id=actor_id, action="register_carrier_slot",
            table="carrier_slots", resource_type="carrier_slot",
            identifier_field="slot_id", identifier=slot_id,
            parent_check="SELECT 1 FROM carriers WHERE carrier_id=?", parent_params=(carrier_id,),
            columns="slot_id,carrier_id,starts_at,ends_at,capacity_json,status,created_at",
            params=(slot_id, carrier_id, fmt(start), fmt(end), canonical_json(cleaned), "open", now),
            audit_detail={"carrier_id": carrier_id, "starts_at": fmt(start), "ends_at": fmt(end)})

    def register_insurance_policy(self, *, request_id: str, actor_id: str, policy_id: str, name: str,
                                  handler_actor_id: str, limit_amount: float,
                                  starts_at: str, ends_at: str) -> dict[str, Any]:
        start = parse_ts(starts_at, "starts_at")
        end = parse_ts(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("保单结束时间必须晚于开始时间")
        limit = _require_number(limit_amount, "limit_amount")
        payload = {"policy_id": policy_id, "name": name, "handler_actor_id": handler_actor_id,
                   "limit_amount": limit, "starts_at": fmt(start), "ends_at": fmt(end)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以登记保单")
            policy_id = _require_str(policy_id, "policy_id")
            name = _require_str(name, "name")
            self._actor(conn, handler_actor_id)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO insurance_policies(policy_id,name,handler_actor_id,limit_amount,"
                        "starts_at,ends_at,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (policy_id, name, handler_actor_id, limit,
                         fmt(start), fmt(end), "active", self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("保单编号已存在") from exc
                append_event(conn, actor_id=actor_id, action="insurance_policy.registered",
                             resource_type="insurance_policy", resource_id=policy_id,
                             detail={"name": name, "limit_amount": limit}, occurred_at=self._now_str())
                return "insurance_policy", policy_id, {"policy_id": policy_id}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="register_insurance_policy",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    # --------------------------------------------------------- 场馆窗口变动入口

    def shorten_venue_window(self, *, request_id: str, actor_id: str, window_id: str,
                             new_starts_at: str, new_ends_at: str,
                             reason: str, notice_confirmed: bool = False) -> dict[str, Any]:
        """场馆缩短开放时段，自动识别在展计划并重排受影响停点。"""

        return self._change_venue_window(
            request_id=request_id, actor_id=actor_id, window_id=window_id,
            new_starts_at=new_starts_at, new_ends_at=new_ends_at, reason=reason,
            close=False, notice_confirmed=notice_confirmed)

    def close_venue(self, *, request_id: str, actor_id: str, venue_id: str, reason: str,
                    notice_confirmed: bool = False) -> dict[str, Any]:
        """场馆临时闭馆，正在生效的计划必须切换到备用场馆。"""

        payload = {"venue_id": venue_id, "reason": reason, "notice_confirmed": notice_confirmed}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            venue = self._load_venue(conn, venue_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR) and actor.actor_id != venue["contact_actor_id"]:
                raise PermissionDenied("只有场馆联系人或运营人员可以申报闭馆")
            windows = conn.execute("SELECT * FROM venue_windows WHERE venue_id=? AND status!='closed'",
                                   (venue_id,)).fetchall()
            if not windows:
                raise NotFoundError("该场馆没有开放中的窗口")
            created: list[dict[str, Any]] = []

            def create():
                now = self._now_str()
                window_ids = [w["window_id"] for w in windows]
                for window in windows:
                    conn.execute(
                        "UPDATE venue_windows SET status='closed', version=version+1, updated_at=? WHERE window_id=?",
                        (now, window["window_id"]))
                    append_event(conn, actor_id=actor_id, action="venue.closed",
                                 resource_type="venue", resource_id=venue_id,
                                 detail={"window_id": window["window_id"], "reason": reason},
                                 occurred_at=now)
                created: list[dict[str, Any]] = []
                # 同一计划受闭馆影响的多个停点合并为一次重排，避免重排草案互相阻塞
                for effective in conn.execute("SELECT plan_id FROM plan_effective").fetchall():
                    affected: list[str] = []
                    for window_id in window_ids:
                        affected.extend(self._plans_using_window(conn, effective["plan_id"], window_id))
                    affected = sorted(set(affected))
                    if not affected:
                        continue
                    if self._has_open_incident(conn, effective["plan_id"], "venue_closed", venue_id):
                        continue
                    try:
                        reroute = self._record_incident_and_reroute(
                            conn, actor_id=actor_id, plan_id=effective["plan_id"], kind="venue_closed",
                            stop_keys=affected, segment_keys=[],
                            detail={"venue_id": venue_id, "window_ids": window_ids,
                                    "reason": reason},
                            notice_confirmed=notice_confirmed)
                    except ConflictError as exc:
                        self._record_reroute_failure(conn, effective["plan_id"], None, exc, now)
                        reroute = {"plan_id": effective["plan_id"], "status": "open",
                                   "reroute": None, "reason": str(exc)}
                    created.append(reroute)
                return "venue", venue_id, {"venue_id": venue_id, "reroutes": created}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="close_venue", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _change_venue_window(self, *, request_id: str, actor_id: str, window_id: str,
                             new_starts_at: str, new_ends_at: str, reason: str,
                             close: bool, notice_confirmed: bool) -> dict[str, Any]:
        new_start = parse_ts(new_starts_at, "new_starts_at")
        new_end = parse_ts(new_ends_at, "new_ends_at")
        if new_end <= new_start:
            raise ValidationError("新窗口结束时间必须晚于开始时间")
        payload = {"window_id": window_id, "new_starts_at": fmt(new_start),
                   "new_ends_at": fmt(new_end), "reason": reason,
                   "notice_confirmed": notice_confirmed, "close": close}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            window = conn.execute("SELECT * FROM venue_windows WHERE window_id=?", (window_id,)).fetchone()
            if window is None:
                raise NotFoundError("场馆窗口不存在")
            venue = self._load_venue(conn, window["venue_id"])
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR) and actor.actor_id != venue["contact_actor_id"]:
                raise PermissionDenied("只有场馆联系人或运营人员可以调整窗口")

            def create():
                now = self._now_str()
                status = "closed" if close else "shortened"
                conn.execute(
                    "UPDATE venue_windows SET starts_at=?, ends_at=?, status=?, version=version+1, updated_at=? "
                    "WHERE window_id=?",
                    (fmt(new_start), fmt(new_end), status, now, window_id))
                append_event(conn, actor_id=actor_id, action="venue_window.shortened",
                             resource_type="venue_window", resource_id=window_id,
                             detail={"new_starts_at": fmt(new_start), "new_ends_at": fmt(new_end),
                                     "reason": reason}, occurred_at=now)
                reroutes = []
                for row in conn.execute("SELECT plan_id FROM plan_effective").fetchall():
                    affected = self._plans_using_window(conn, row["plan_id"], window_id)
                    if affected and not self._has_open_incident(conn, row["plan_id"], "window_shortened", window_id):
                        try:
                            reroute = self._record_incident_and_reroute(
                                conn, actor_id=actor_id, plan_id=row["plan_id"], kind="window_shortened",
                                stop_keys=affected, segment_keys=[],
                                detail={"window_id": window_id, "new_starts_at": fmt(new_start),
                                        "new_ends_at": fmt(new_end), "reason": reason},
                                notice_confirmed=notice_confirmed)
                        except ConflictError as exc:
                            self._record_reroute_failure(conn, row["plan_id"], None, exc, now)
                            reroute = {"plan_id": row["plan_id"], "status": "open",
                                       "reroute": None, "reason": str(exc)}
                        reroutes.append(reroute)
                return "venue_window", window_id, {"window_id": window_id, "reroutes": reroutes}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="shorten_venue_window",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ------------------------------------------------------------- 草案与校验

    def create_draft(self, *, request_id: str, actor_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """根据计划清单生成限时草案，占用全部所需资源。"""

        manifest = _require_dict(manifest, "manifest")
        payload = {"actor_id": actor_id, "manifest": manifest}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以编排巡展计划")
            self.sweep_expired(conn)
            normalized = self._normalize_manifest(conn, manifest)
            plan_id = normalized["plan_id"]
            lease_seconds = normalized["lease_seconds"]
            lease_expires = self._now() + timedelta(seconds=lease_seconds)

            def create():
                if conn.execute("SELECT 1 FROM tour_plans WHERE plan_id=? AND status=?",
                                (plan_id, DRAFT)).fetchone():
                    raise ConflictError("该计划存在尚未完成确认的草案")
                parent_row = conn.execute(
                    "SELECT version FROM plan_effective WHERE plan_id=?", (plan_id,)).fetchone()
                parent_version = parent_row["version"] if parent_row else None
                version = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM tour_plans WHERE plan_id=?",
                    (plan_id,)).fetchone()["v"]
                content_hash = digest(strip_datetimes(normalized))
                conflicts = self._detect_conflicts(conn, normalized, plan_id)
                status = BLOCKED if conflicts else DRAFT
                conn.execute(
                    "INSERT INTO tour_plans(plan_id,version,status,rep_actor_id,manifest_json,"
                    "parent_version,content_hash,lease_expires_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, version, status, normalized["rep_actor_id"],
                     canonical_json(strip_datetimes(normalized)),
                     parent_version, content_hash,
                     fmt(lease_expires) if status == DRAFT else None,
                     actor_id, self._now_str()))
                self._write_snapshots(conn, plan_id, version, normalized)
                self._write_allocations(conn, plan_id, version, normalized,
                                        lease_expires if status == DRAFT else None,
                                        blocked=bool(conflicts))
                if conflicts:
                    conn.execute(
                        "INSERT INTO plan_conflicts(plan_id,version,detail_json,created_at) VALUES(?,?,?,?)",
                        (plan_id, version, canonical_json(
                            {"kind": "resource_conflict", "items": conflicts,
                             "recovery": ["按 options 顺序切换备用场馆或承运方案",
                                          "等待其他计划的限时租约到期后重试"]}),
                         self._now_str()))
                else:
                    self._build_confirmations(conn, plan_id, version, normalized,
                                             changed_stops=None, changed_segments=None)
                append_event(conn, actor_id=actor_id,
                             action="tour.draft_blocked" if conflicts else "tour.draft_created",
                             resource_type="tour_plan", resource_id=plan_id,
                             detail={"version": version, "content_hash": content_hash,
                                     "conflicts": len(conflicts),
                                     "lease_expires_at": fmt(lease_expires) if not conflicts else None},
                             occurred_at=self._now_str())
                response = {"plan_id": plan_id, "version": version, "status": status,
                            "lease_expires_at": fmt(lease_expires) if not conflicts else None,
                            "conflicts": conflicts}
                return "tour_plan", plan_id, response

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="create_tour_draft",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _load_venue(self, conn, venue_id: str):
        row = conn.execute("SELECT * FROM venues WHERE venue_id=?", (venue_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"场馆 {venue_id} 不存在")
        return row

    def _normalize_manifest(self, conn, manifest: dict[str, Any]) -> dict[str, Any]:
        """把外部清单转换为经过校验的规范化内部结构。"""

        plan_id = _require_str(manifest.get("plan_id"), "plan_id")
        artwork_ids = manifest.get("artwork_ids")
        if not isinstance(artwork_ids, list) or not artwork_ids:
            raise ValidationError("artwork_ids 必须是非空数组")
        artworks: dict[str, dict[str, Any]] = {}
        for artwork_id in artwork_ids:
            row = conn.execute("SELECT * FROM artworks WHERE artwork_id=?", (artwork_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"作品 {artwork_id} 不存在")
            import json as _json
            artworks[artwork_id] = {"artwork_id": artwork_id, "title": row["title"],
                                    "representative_id": row["representative_id"],
                                    "declared_value": row["declared_value"],
                                    "spec": _json.loads(row["spec_json"])}
        rep_ids = {item["representative_id"] for item in artworks.values()}
        rep_actor_id = _require_str(manifest.get("rep_actor_id"), "rep_actor_id")
        rep = self._actor(conn, rep_actor_id)
        if rep.role not in (ROLE_REP, ROLE_ADMIN) or rep_actor_id not in rep_ids:
            raise ValidationError("rep_actor_id 必须是清单内作品的代表")
        lease_seconds = manifest.get("lease_seconds", LEASE_DEFAULT_SECONDS)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not (30 <= lease_seconds <= 86400):
            raise ValidationError("lease_seconds 必须是 30..86400 的整数")

        stops_raw = manifest.get("stops")
        segments_raw = manifest.get("segments")
        insurance_raw = manifest.get("insurance")
        if not isinstance(stops_raw, list) or not stops_raw:
            raise ValidationError("stops 必须是非空数组")
        if not isinstance(segments_raw, list) or len(segments_raw) != len(stops_raw):
            raise ValidationError("每个停点必须对应一条进场上场运输区段")
        if not isinstance(insurance_raw, list) or not insurance_raw:
            raise ValidationError("insurance 必须是非空数组")

        stops: list[dict[str, Any]] = []
        stop_keys: set[str] = set()
        prev_dismantle: datetime | None = None
        for index, raw_stop in enumerate(stops_raw):
            stop = self._normalize_stop(conn, raw_stop, artworks)
            if stop["stop_key"] in stop_keys:
                raise ValidationError(f"停点键 {stop['stop_key']} 重复")
            stop_keys.add(stop["stop_key"])
            if prev_dismantle and stop["chosen"]["install_at_dt"] < prev_dismantle:
                raise ValidationError(f"停点 {stop['stop_key']} 的布展时间早于上一停点撤展完成")
            prev_dismantle = stop["chosen"]["dismantle_at_dt"]
            stops.append(stop)

        segments: list[dict[str, Any]] = []
        segment_keys: set[str] = set()
        for index, raw_segment in enumerate(segments_raw):
            segment = self._normalize_segment(conn, raw_segment, artworks)
            if segment["segment_key"] in segment_keys:
                raise ValidationError(f"区段键 {segment['segment_key']} 重复")
            segment_keys.add(segment["segment_key"])
            target = stops[index]
            if segment["to_stop_key"] != target["stop_key"]:
                raise ValidationError(
                    f"区段 {segment['segment_key']} 的到达停点必须是顺序中的 {target['stop_key']}")
            if index == 0:
                if segment.get("from_stop_key"):
                    raise ValidationError("第一条运输区段必须从仓库出发（from_stop_key 留空）")
            elif segment["from_stop_key"] != stops[index - 1]["stop_key"]:
                raise ValidationError(f"区段 {segment['segment_key']} 的始发停点链接错误")
            if segment["chosen"]["deliver_at_dt"] > target["chosen"]["install_at_dt"]:
                raise ValidationError(
                    f"区段 {segment['segment_key']} 送达时间晚于 {target['stop_key']} 布展开始")
            if index > 0 and segment["chosen"]["pickup_at_dt"] < stops[index - 1]["chosen"]["dismantle_at_dt"]:
                raise ValidationError(
                    f"区段 {segment['segment_key']} 提货时间早于上一停点撤展完成")
            segments.append(segment)

        insurance = self._normalize_insurance(conn, insurance_raw, artworks, segments)
        return {
            "plan_id": plan_id,
            "artwork_ids": list(artworks),
            "artworks": artworks,
            "rep_actor_id": rep_actor_id,
            "lease_seconds": lease_seconds,
            "stops": stops,
            "segments": segments,
            "insurance": insurance,
        }

    def _normalize_stop(self, conn, raw: Any, artworks: dict[str, Any]) -> dict[str, Any]:
        raw = _require_dict(raw, "stop")
        stop_key = _require_str(raw.get("stop_key"), "stop_key", 60)
        options_raw = raw.get("options")
        if not isinstance(options_raw, list) or not options_raw:
            raise ValidationError(f"停点 {stop_key} 至少需要一个 options 选项")
        sessions = self._normalize_sessions(stop_key, raw.get("sessions", []))
        chosen_index = int(raw.get("chosen_index", 0))
        options = [self._normalize_stop_option(conn, stop_key, index, item,
                                               strict=(index == chosen_index))
                   for index, item in enumerate(options_raw)]
        if not 0 <= chosen_index < len(options):
            raise ValidationError(f"停点 {stop_key} 的 chosen_index 越界")
        chosen = options[chosen_index]
        for session in sessions:
            if not (chosen["open_at_dt"] <= session["starts_at_dt"] < session["ends_at_dt"]
                    <= chosen["close_at_dt"]):
                raise ValidationError(
                    f"停点 {stop_key} 的观众场次 {session['title']} 不在主选场馆开放时段内")
        publicity = None
        if raw.get("publicity") is not None:
            pub = _require_dict(raw["publicity"], "publicity")
            publicity = {
                "commitment": _require_str(pub.get("commitment"), "publicity.commitment", 300),
                "deadline": fmt(parse_ts(pub.get("deadline"), "publicity.deadline")),
            }
        return {"stop_key": stop_key, "options": options, "chosen_index": chosen_index,
                "chosen": chosen, "sessions": sessions, "publicity": publicity}

    def _normalize_stop_option(self, conn, stop_key: str, index: int, raw: Any,
                               *, strict: bool) -> dict[str, Any]:
        prefix = f"stops[{stop_key}].options[{index}]"
        raw = _require_dict(raw, prefix)
        venue_id = _require_str(raw.get("venue_id"), f"{prefix}.venue_id")
        window_id = _require_str(raw.get("window_id"), f"{prefix}.window_id")
        case_id = _require_str(raw.get("case_id"), f"{prefix}.case_id")
        labor_id = _require_str(raw.get("labor_id"), f"{prefix}.labor_id")
        receiver_id = _require_str(raw.get("receiver_id"), f"{prefix}.receiver_id")
        venue = self._load_venue(conn, venue_id)
        window = conn.execute("SELECT * FROM venue_windows WHERE window_id=? AND venue_id=?",
                              (window_id, venue_id)).fetchone()
        if window is None:
            raise NotFoundError(f"{prefix}: 窗口不属于该场馆或不存在")
        case = conn.execute("SELECT * FROM display_cases WHERE case_id=? AND venue_id=?",
                            (case_id, venue_id)).fetchone()
        labor = conn.execute("SELECT * FROM venue_labor_slots WHERE labor_id=? AND venue_id=?",
                             (labor_id, venue_id)).fetchone()
        receiver = conn.execute("SELECT * FROM receivers WHERE receiver_id=? AND venue_id=? AND active=1",
                                (receiver_id, venue_id)).fetchone()
        if case is None:
            raise NotFoundError(f"{prefix}: 展柜不存在于该场馆")
        if labor is None:
            raise NotFoundError(f"{prefix}: 工时区段不存在于该场馆")
        if receiver is None:
            raise NotFoundError(f"{prefix}: 该场馆没有有效接收人")
        if receiver["qualification"] != venue["required_qualification"]:
            raise ValidationError(
                f"{prefix}: 接收资质 {receiver['qualification']} 不满足场馆要求 {venue['required_qualification']}")
        install = parse_ts(raw.get("install_at"), f"{prefix}.install_at")
        open_at = parse_ts(raw.get("open_at"), f"{prefix}.open_at")
        close_at = parse_ts(raw.get("close_at"), f"{prefix}.close_at")
        dismantle = parse_ts(raw.get("dismantle_at"), f"{prefix}.dismantle_at")
        if not (install < open_at < close_at < dismantle):
            raise ValidationError(f"{prefix}: 时间顺序必须是 布展<开放<闭馆<撤展")
        window_start = parse_ts(window["starts_at"], "window.starts_at")
        window_end = parse_ts(window["ends_at"], "window.ends_at")
        if strict and window["status"] != "open":
            raise ValidationError(f"{prefix}: 选中场馆窗口状态为 {window['status']}")
        if strict and not (window_start <= install and dismantle <= window_end):
            raise ValidationError(f"{prefix}: 布撤展区间必须落在选中场馆窗口内")
        labor_start = parse_ts(labor["starts_at"], "labor.starts_at")
        labor_end = parse_ts(labor["ends_at"], "labor.ends_at")
        if strict and not (labor_start <= install and dismantle <= labor_end):
            raise ValidationError(f"{prefix}: 布撤展区间必须落在可用工时区段内")
        cost = _require_number(raw.get("cost", 0), f"{prefix}.cost")
        return {
            "option_index": index, "venue_id": venue_id, "window_id": window_id,
            "case_id": case_id, "labor_id": labor_id, "receiver_id": receiver_id,
            "qualification": receiver["qualification"],
            "install_at": fmt(install), "open_at": fmt(open_at),
            "close_at": fmt(close_at), "dismantle_at": fmt(dismantle),
            "install_at_dt": install, "open_at_dt": open_at,
            "close_at_dt": close_at, "dismantle_at_dt": dismantle,
            "window_start": fmt(window_start), "window_end": fmt(window_end),
            "labor_hours": labor["available_hours"], "cost": cost,
        }

    def _normalize_sessions(self, stop_key: str, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise ValidationError(f"停点 {stop_key} 的 sessions 必须是数组")
        sessions = []
        for index, item in enumerate(raw):
            item = _require_dict(item, f"stops[{stop_key}].sessions[{index}]")
            start = parse_ts(item.get("starts_at"), "session.starts_at")
            end = parse_ts(item.get("ends_at"), "session.ends_at")
            if end <= start:
                raise ValidationError("观众场次结束时间必须晚于开始时间")
            sessions.append({
                "session_key": _require_str(item.get("session_key"), "session_key", 60),
                "title": _require_str(item.get("title"), "session.title", 120),
                "starts_at": fmt(start), "ends_at": fmt(end),
                "starts_at_dt": start, "ends_at_dt": end,
                "announce": bool(item.get("announce", True)),
            })
        keys = [s["session_key"] for s in sessions]
        if len(set(keys)) != len(keys):
            raise ValidationError(f"停点 {stop_key} 的场次键重复")
        return sessions

    def _normalize_segment(self, conn, raw: Any, artworks: dict[str, Any]) -> dict[str, Any]:
        raw = _require_dict(raw, "segment")
        segment_key = _require_str(raw.get("segment_key"), "segment_key", 60)
        to_stop_key = _require_str(raw.get("to_stop_key"), "to_stop_key", 60)
        from_stop_key = raw.get("from_stop_key") or None
        options_raw = raw.get("options")
        if not isinstance(options_raw, list) or not options_raw:
            raise ValidationError(f"区段 {segment_key} 至少需要一个 options 选项")
        chosen_index = int(raw.get("chosen_index", 0))
        options = [self._normalize_segment_option(conn, segment_key, index, item, artworks,
                                                  strict=(index == chosen_index))
                   for index, item in enumerate(options_raw)]
        if not 0 <= chosen_index < len(options):
            raise ValidationError(f"区段 {segment_key} 的 chosen_index 越界")
        return {"segment_key": segment_key, "from_stop_key": from_stop_key,
                "to_stop_key": to_stop_key, "options": options, "chosen_index": chosen_index,
                "chosen": options[chosen_index]}

    def _normalize_segment_option(self, conn, segment_key: str, index: int, raw: Any,
                                  artworks: dict[str, Any], *, strict: bool) -> dict[str, Any]:
        prefix = f"segments[{segment_key}].options[{index}]"
        raw = _require_dict(raw, prefix)
        carrier_id = _require_str(raw.get("carrier_id"), f"{prefix}.carrier_id")
        slot_id = _require_str(raw.get("slot_id"), f"{prefix}.slot_id")
        carrier = conn.execute("SELECT * FROM carriers WHERE carrier_id=?", (carrier_id,)).fetchone()
        if carrier is None:
            raise NotFoundError(f"{prefix}: 承运方不存在")
        slot = conn.execute("SELECT * FROM carrier_slots WHERE slot_id=? AND carrier_id=?",
                            (slot_id, carrier_id)).fetchone()
        if slot is None:
            raise NotFoundError(f"{prefix}: 承运区段不属于该承运方或不存在")
        if strict and slot["status"] != "open":
            raise ValidationError(f"{prefix}: 选中承运区段状态为 {slot['status']}")
        pickup = parse_ts(raw.get("pickup_at"), f"{prefix}.pickup_at")
        deliver = parse_ts(raw.get("deliver_at"), f"{prefix}.deliver_at")
        if deliver <= pickup:
            raise ValidationError(f"{prefix}: 送达必须晚于提货")
        slot_start = parse_ts(slot["starts_at"], "slot.starts_at")
        slot_end = parse_ts(slot["ends_at"], "slot.ends_at")
        if strict and not (slot_start <= pickup and deliver <= slot_end):
            raise ValidationError(f"{prefix}: 运输时间必须落在选中承运区段内")
        import json as _json
        capacity = _json.loads(slot["capacity_json"])
        total_weight = sum(item["spec"]["weight_kg"] for item in artworks.values())
        total_volume = sum(item["spec"]["volume_m3"] for item in artworks.values())
        total_pieces = sum(item["spec"]["pieces"] for item in artworks.values())
        if total_weight > capacity["max_weight_kg"]:
            raise ValidationError(f"{prefix}: 总重量 {total_weight} 超过承运能力 {capacity['max_weight_kg']}")
        if total_volume > capacity["max_volume_m3"]:
            raise ValidationError(f"{prefix}: 总体积 {total_volume} 超过承运能力 {capacity['max_volume_m3']}")
        if total_pieces > capacity["max_pieces"]:
            raise ValidationError(f"{prefix}: 总件数 {total_pieces} 超过承运能力 {capacity['max_pieces']}")
        cost = _require_number(raw.get("cost", 0), f"{prefix}.cost")
        return {
            "option_index": index, "carrier_id": carrier_id, "slot_id": slot_id,
            "pickup_at": fmt(pickup), "deliver_at": fmt(deliver),
            "pickup_at_dt": pickup, "deliver_at_dt": deliver,
            "total_weight_kg": total_weight, "total_volume_m3": total_volume,
            "total_pieces": total_pieces, "cost": cost,
        }

    def _normalize_insurance(self, conn, raw: list, artworks: dict[str, Any],
                             segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cover_from = min(seg["chosen"]["pickup_at_dt"] for seg in segments)
        cover_to = max(seg["chosen"]["deliver_at_dt"] for seg in segments)
        assignments: list[dict[str, Any]] = []
        covered: set[str] = set()
        seen_policies: set[str] = set()
        for index, item in enumerate(raw):
            item = _require_dict(item, f"insurance[{index}]")
            policy_id = _require_str(item.get("policy_id"), "insurance.policy_id")
            if policy_id in seen_policies:
                raise ValidationError(f"保单 {policy_id} 在计划中重复出现")
            seen_policies.add(policy_id)
            policy = conn.execute("SELECT * FROM insurance_policies WHERE policy_id=?",
                                  (policy_id,)).fetchone()
            if policy is None:
                raise NotFoundError(f"保单 {policy_id} 不存在")
            if policy["status"] != "active":
                raise ValidationError(f"保单 {policy_id} 状态为 {policy['status']}")
            ids = item.get("artwork_ids")
            if not isinstance(ids, list) or not ids:
                raise ValidationError(f"保单 {policy_id} 必须指定覆盖作品")
            amount = 0.0
            for artwork_id in ids:
                if artwork_id not in artworks:
                    raise ValidationError(f"保单 {policy_id} 覆盖了清单外作品 {artwork_id}")
                if artwork_id in covered:
                    raise ValidationError(f"作品 {artwork_id} 被多张保单重复投保")
                covered.add(artwork_id)
                amount += float(artworks[artwork_id]["declared_value"])
            starts = parse_ts(item.get("cover_from"), "insurance.cover_from")
            ends = parse_ts(item.get("cover_to"), "insurance.cover_to")
            if starts > cover_from or ends < cover_to:
                raise ValidationError(
                    f"保单 {policy_id} 的覆盖时段必须包含全程运输 {fmt(cover_from)} ~ {fmt(cover_to)}")
            policy_start = parse_ts(policy["starts_at"], "policy.starts_at")
            policy_end = parse_ts(policy["ends_at"], "policy.ends_at")
            if not (policy_start <= starts and ends <= policy_end):
                raise ValidationError(f"保单 {policy_id} 投保时间超出保单有效期")
            cost = _require_number(item.get("cost", 0), "insurance.cost")
            assignments.append({"policy_id": policy_id, "artwork_ids": list(ids),
                                "amount": amount, "cover_from": fmt(starts),
                                "cover_to": fmt(ends), "cover_from_dt": starts,
                                "cover_to_dt": ends, "cost": cost})
        if covered != set(artworks):
            raise ValidationError("存在未投保的作品")
        return assignments

    # ------------------------------------------------------------- 资源占用

    def _iter_desired_allocations(self, normalized: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for stop in normalized["stops"]:
            chosen = stop["chosen"]
            interval = {"starts_at": chosen["install_at"], "ends_at": chosen["dismantle_at"],
                        "starts_dt": chosen["install_at_dt"], "ends_dt": chosen["dismantle_at_dt"]}
            yield {"key": f"window:{chosen['window_id']}", "resource_type": "venue_window",
                   "resource_id": chosen["window_id"], **interval, "amount": None,
                   "stop_key": stop["stop_key"]}
            yield {"key": f"case:{chosen['case_id']}", "resource_type": "display_case",
                   "resource_id": chosen["case_id"], **interval, "amount": None,
                   "stop_key": stop["stop_key"]}
            yield {"key": f"labor:{chosen['labor_id']}", "resource_type": "venue_labor",
                   "resource_id": chosen["labor_id"], **interval,
                   "amount": sum(item["spec"]["install_hours"]
                                 for item in normalized["artworks"].values()),
                   "stop_key": stop["stop_key"]}
        for segment in normalized["segments"]:
            chosen = segment["chosen"]
            yield {"key": f"slot:{chosen['slot_id']}", "resource_type": "carrier_slot",
                   "resource_id": chosen["slot_id"],
                   "starts_at": chosen["pickup_at"], "ends_at": chosen["deliver_at"],
                   "starts_dt": chosen["pickup_at_dt"], "ends_dt": chosen["deliver_at_dt"],
                   "amount": chosen["total_weight_kg"], "segment_key": segment["segment_key"]}
        for insurance in normalized["insurance"]:
            yield {"key": f"policy:{insurance['policy_id']}", "resource_type": "insurance_policy",
                   "resource_id": insurance["policy_id"],
                   "starts_at": insurance["cover_from"], "ends_at": insurance["cover_to"],
                   "starts_dt": insurance["cover_from_dt"], "ends_dt": insurance["cover_to_dt"],
                   "amount": insurance["amount"], "insurance": True}

    def _detect_conflicts(self, conn, normalized: dict[str, Any], plan_id: str) -> list[dict[str, Any]]:
        """检查跨计划双重预订、展柜条件、工时、承运能力与保险额度。"""

        conflicts: list[dict[str, Any]] = []
        import json as _json
        # 展柜物理条件
        for stop in normalized["stops"]:
            chosen = stop["chosen"]
            case = conn.execute("SELECT * FROM display_cases WHERE case_id=?",
                                (chosen["case_id"],)).fetchone()
            conditions = _json.loads(case["conditions_json"])
            totals = {
                "weight_kg": sum(item["spec"]["weight_kg"] for item in normalized["artworks"].values()),
                "volume_m3": sum(item["spec"]["volume_m3"] for item in normalized["artworks"].values()),
            }
            needs_climate = any(item["spec"]["climate"] for item in normalized["artworks"].values())
            needs_security = any(item["spec"]["security"] for item in normalized["artworks"].values())
            if totals["weight_kg"] > conditions["max_weight_kg"]:
                conflicts.append(self._conflict_item(
                    "case_weight", stop["stop_key"], "display_case", chosen["case_id"],
                    f"展品总重 {totals['weight_kg']} 超过展柜承重 {conditions['max_weight_kg']}"))
            if totals["volume_m3"] > conditions["max_volume_m3"]:
                conflicts.append(self._conflict_item(
                    "case_volume", stop["stop_key"], "display_case", chosen["case_id"],
                    f"展品总体积 {totals['volume_m3']} 超过展柜容积 {conditions['max_volume_m3']}"))
            if needs_climate and not conditions["climate"]:
                conflicts.append(self._conflict_item(
                    "case_climate", stop["stop_key"], "display_case", chosen["case_id"],
                    "展品要求恒湿恒温但展柜不具备"))
            if needs_security and not conditions["security"]:
                conflicts.append(self._conflict_item(
                    "case_security", stop["stop_key"], "display_case", chosen["case_id"],
                    "展品要求安保条件但展柜不具备"))
            if sum(item["spec"]["install_hours"]
                   for item in normalized["artworks"].values()) > chosen["labor_hours"]:
                conflicts.append(self._conflict_item(
                    "labor_hours", stop["stop_key"], "venue_labor", chosen["labor_id"],
                    "布撤展总工时超过场馆可用工时"))

        desired = list(self._iter_desired_allocations(normalized))
        for desired_item in desired:
            rows = conn.execute(
                "SELECT * FROM resource_allocations WHERE resource_type=? AND resource_id=? "
                "AND status IN (?,?) AND plan_id!=? ORDER BY starts_at",
                (desired_item["resource_type"], desired_item["resource_id"],
                 HELD, CONFIRMED, plan_id)).fetchall()
            for row in rows:
                if self._overlaps(row["starts_at"], row["ends_at"],
                                  desired_item["starts_at"], desired_item["ends_at"]):
                    lease_note = ""
                    if row["status"] == HELD:
                        lease_note = f"（对方为限时草案，租约到期 {row['lease_expires_at']}）"
                    # 保险按额度累加，其余资源时间互斥
                    if desired_item["resource_type"] == "insurance_policy":
                        active_amount = self._active_insurance_amount(
                            conn, desired_item["resource_id"], desired_item["starts_at"],
                            desired_item["ends_at"], plan_id)
                        policy = conn.execute("SELECT limit_amount FROM insurance_policies WHERE policy_id=?",
                                              (desired_item["resource_id"],)).fetchone()
                        if active_amount + (desired_item["amount"] or 0) <= policy["limit_amount"]:
                            continue
                        reason = (f"投保额度不足：在保 {active_amount} + 本计划 {desired_item['amount']} "
                                  f"超过额度 {policy['limit_amount']}{lease_note}")
                    else:
                        reason = f"时段 {desired_item['starts_at']}~{desired_item['ends_at']} 与计划 {row['plan_id']} 占用重叠{lease_note}"
                    conflicts.append({
                        "code": "double_booking",
                        "resource_type": desired_item["resource_type"],
                        "resource_id": desired_item["resource_id"],
                        "stop_key": desired_item.get("stop_key"),
                        "segment_key": desired_item.get("segment_key"),
                        "wanted": {"starts_at": desired_item["starts_at"],
                                   "ends_at": desired_item["ends_at"],
                                   "amount": desired_item["amount"]},
                        "blocking_plan_id": row["plan_id"],
                        "blocking_version": row["version"],
                        "blocking_status": row["status"],
                        "lease_expires_at": row["lease_expires_at"],
                        "reason": reason,
                        "recovery": self._recovery_hint(normalized, desired_item),
                    })
        return conflicts

    def _conflict_item(self, code: str, stop_key: str, resource_type: str,
                       resource_id: str, reason: str) -> dict[str, Any]:
        return {"code": code, "stop_key": stop_key, "segment_key": None,
                "resource_type": resource_type, "resource_id": resource_id,
                "reason": reason,
                "recovery": ["按 options 顺序切换到该停点的备用场馆"]}

    def _recovery_hint(self, normalized: dict[str, Any], desired_item: dict[str, Any]) -> list[str]:
        hints = []
        if desired_item.get("stop_key"):
            stop = next(s for s in normalized["stops"] if s["stop_key"] == desired_item["stop_key"])
            hints += [f"停点 {stop['stop_key']} 切换到备用选项 index={i}"
                      for i in range(1, len(stop["options"]))]
        if desired_item.get("segment_key"):
            segment = next(s for s in normalized["segments"] if s["segment_key"] == desired_item["segment_key"])
            hints += [f"区段 {segment['segment_key']} 切换到备用承运 index={i}"
                      for i in range(1, len(segment["options"]))]
        if desired_item["resource_type"] == "insurance_policy":
            hints.append("改投其他未饱和保单")
        hints.append("等待冲突计划的限时租约到期后重试")
        return hints

    def _active_insurance_amount(self, conn, policy_id: str, starts_at: str, ends_at: str,
                                 exclude_plan: str) -> float:
        total = 0.0
        rows = conn.execute(
            "SELECT * FROM resource_allocations WHERE resource_type='insurance_policy' "
            "AND resource_id=? AND status IN (?,?) AND plan_id!=?",
            (policy_id, HELD, CONFIRMED, exclude_plan)).fetchall()
        for row in rows:
            if self._overlaps(row["starts_at"], row["ends_at"], starts_at, ends_at):
                total += row["amount"] or 0.0
        return total

    @staticmethod
    def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
        return start_a < end_b and start_b < end_a

    def _write_snapshots(self, conn, plan_id: str, version: int, normalized: dict[str, Any]) -> None:
        for stop in normalized["stops"]:
            conn.execute(
                "INSERT INTO plan_stops_snapshot(plan_id,version,stop_key,payload_json) VALUES(?,?,?,?)",
                (plan_id, version, stop["stop_key"], canonical_json(strip_datetimes(stop))))
        for segment in normalized["segments"]:
            conn.execute(
                "INSERT INTO plan_segments_snapshot(plan_id,version,segment_key,payload_json) VALUES(?,?,?,?)",
                (plan_id, version, segment["segment_key"], canonical_json(strip_datetimes(segment))))

    @staticmethod
    def _rehydrate(data: dict[str, Any]) -> dict[str, Any]:
        """把持久化清单恢复为带进程内 datetime 字段的形态，不重新校验资源状态。"""

        import copy
        result = copy.deepcopy(data)
        for stop in result["stops"]:
            chosen_index = stop.get("chosen_index", 0)
            stop["chosen_index"] = chosen_index
            chosen = copy.deepcopy(stop["options"][chosen_index])
            for field in ("install_at", "open_at", "close_at", "dismantle_at"):
                chosen[f"{field}_dt"] = parse_ts(chosen[field], field)
            chosen["window_start_dt"] = parse_ts(chosen["window_start"], "window_start")
            chosen["window_end_dt"] = parse_ts(chosen["window_end"], "window_end")
            stop["chosen"] = chosen
            for session in stop["sessions"]:
                session["starts_at_dt"] = parse_ts(session["starts_at"], "starts_at")
                session["ends_at_dt"] = parse_ts(session["ends_at"], "ends_at")
        for segment in result["segments"]:
            chosen_index = segment.get("chosen_index", 0)
            segment["chosen_index"] = chosen_index
            chosen = copy.deepcopy(segment["options"][chosen_index])
            chosen["pickup_at_dt"] = parse_ts(chosen["pickup_at"], "pickup_at")
            chosen["deliver_at_dt"] = parse_ts(chosen["deliver_at"], "deliver_at")
            segment["chosen"] = chosen
        for insurance in result.get("insurance", []):
            insurance["cover_from_dt"] = parse_ts(insurance["cover_from"], "cover_from")
            insurance["cover_to_dt"] = parse_ts(insurance["cover_to"], "cover_to")
        return result

    def _write_allocations(self, conn, plan_id: str, version: int, normalized: dict[str, Any],
                           lease_expires: datetime | None, *, blocked: bool) -> None:
        now = self._now_str()
        for item in self._iter_desired_allocations(normalized):
            conn.execute(
                "INSERT INTO resource_allocations(allocation_id,plan_id,version,resource_type,"
                "resource_id,starts_at,ends_at,amount,status,lease_expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, version, item["resource_type"], item["resource_id"],
                 item["starts_at"], item["ends_at"], item["amount"],
                 BLOCKED_ALLOC if blocked else HELD,
                 fmt(lease_expires) if lease_expires and not blocked else None, now))

    # ------------------------------------------------------------- 多方确认

    def _build_confirmations(self, conn, plan_id: str, version: int, normalized: dict[str, Any],
                             changed_stops: set[str] | None,
                             changed_segments: set[str] | None) -> None:
        """生成待确认步骤。重排版本只为变动环节生成步骤，顺序保持稳定。

        同一场馆覆盖多个停点、同一承运方承担多个区段、同一经办持有多张保单时，
        合并为一个确认步骤，避免重复确认与多占资源。
        """

        now = self._now_str()
        full = changed_stops is None and changed_segments is None
        steps: list[tuple[str, str, str]] = [(
            PARTY_REPRESENTATIVE, normalized["rep_actor_id"], "作品代表确认全部作品及组件参展")]
        if full or changed_segments:
            policies = sorted({item["policy_id"] for item in normalized["insurance"]})
            for policy_id in policies:
                steps.append((PARTY_INSURANCE, policy_id, "保险经办确认投保额度与覆盖时段"))
        scope_stops = set(normalized["stops"][i]["stop_key"] for i in range(len(normalized["stops"]))) \
            if full else set(changed_stops or set())
        venues: dict[str, list[str]] = {}
        for stop in normalized["stops"]:
            if stop["stop_key"] in scope_stops:
                venues.setdefault(stop["chosen"]["venue_id"], []).append(stop["stop_key"])
        for venue_id in sorted(venues):
            steps.append((PARTY_VENUE, venue_id,
                          f"场馆确认停点 {','.join(sorted(venues[venue_id]))} 的窗口、展柜、工时与接收资质"))
        scope_segments = {s["segment_key"] for s in normalized["segments"]} \
            if full else set(changed_segments or set())
        carriers: dict[str, list[str]] = {}
        for segment in normalized["segments"]:
            if segment["segment_key"] in scope_segments:
                carriers.setdefault(segment["chosen"]["carrier_id"], []).append(segment["segment_key"])
        for carrier_id in sorted(carriers):
            steps.append((PARTY_CARRIER, carrier_id,
                          f"承运方确认区段 {','.join(sorted(carriers[carrier_id]))} 的承运能力与时刻"))
        for step, (kind, ref, instruction) in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO plan_confirmations(plan_id,version,party_kind,party_ref,step,"
                "status,instruction,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, version, kind, ref, step, "pending", instruction, now))

    def _parties_for_actor(self, conn, normalized: dict[str, Any], actor_id: str
                           ) -> list[tuple[str, str]]:
        """返回操作者在该计划中承担的全部确认职责（可能负责多个场馆或承运方）。"""

        bindings: list[tuple[str, str]] = []
        if actor_id == normalized["rep_actor_id"]:
            bindings.append((PARTY_REPRESENTATIVE, normalized["rep_actor_id"]))
        policy_rows = conn.execute(
            "SELECT policy_id FROM insurance_policies WHERE handler_actor_id=?",
            (actor_id,)).fetchall()
        policy_ids = {item["policy_id"] for item in normalized["insurance"]}
        for row in policy_rows:
            if row["policy_id"] in policy_ids:
                bindings.append((PARTY_INSURANCE, row["policy_id"]))
        venue_rows = conn.execute(
            "SELECT venue_id FROM venues WHERE contact_actor_id=?", (actor_id,)).fetchall()
        venue_ids = {stop["chosen"]["venue_id"] for stop in normalized["stops"]}
        for row in venue_rows:
            if row["venue_id"] in venue_ids:
                bindings.append((PARTY_VENUE, row["venue_id"]))
        carrier_rows = conn.execute(
            "SELECT carrier_id FROM carriers WHERE contact_actor_id=?", (actor_id,)).fetchall()
        carrier_ids = {segment["chosen"]["carrier_id"] for segment in normalized["segments"]}
        for row in carrier_rows:
            if row["carrier_id"] in carrier_ids:
                bindings.append((PARTY_CARRIER, row["carrier_id"]))
        return bindings

    def confirm(self, *, request_id: str, actor_id: str, plan_id: str, version: int,
                approved: bool = True, comment: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "version": version,
                   "approved": approved, "comment": comment}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self.sweep_expired(conn)
            plan = self._load_plan_version(conn, plan_id, version)
            import json as _json
            normalized = _json.loads(plan["manifest_json"])
            bindings = self._parties_for_actor(conn, normalized, actor_id)
            if not bindings and actor.role not in (ROLE_ADMIN,):
                raise PermissionDenied("当前操作者与该计划的确认职责无关")
            if actor.role == ROLE_AUDITOR:
                raise PermissionDenied("审计员不能参与业务确认")

            def create():
                if plan["status"] != DRAFT:
                    raise ConflictError(f"版本 {version} 当前状态 {plan['status']}，不能确认")
                keys = [(kind, ref) for kind, ref in bindings]
                clauses = " OR ".join("(party_kind=? AND party_ref=?)" for _ in keys)
                flat = [v for pair in keys for v in pair]
                mine = conn.execute(
                    f"SELECT * FROM plan_confirmations WHERE plan_id=? AND version=? "
                    f"AND ({clauses}) ORDER BY step",
                    [plan_id, version, *flat]).fetchall() if keys else []
                pending_rows = [r for r in mine if r["status"] == "pending"]
                if not pending_rows:
                    if mine:
                        latest = mine[-1]
                        # 重复确认：原样返回，不延长租约、不重复占用资源
                        return "tour_confirmation", f"{plan_id}:{version}:{latest['party_kind']}:{latest['party_ref']}", {
                            "plan_id": plan_id, "version": version,
                            "party_kind": latest["party_kind"], "party_ref": latest["party_ref"],
                            "status": latest["status"], "duplicate": True}
                    raise PermissionDenied("当前版本没有分配给该方的待确认步骤")
                current = conn.execute(
                    "SELECT MIN(step) AS s FROM plan_confirmations WHERE plan_id=? AND version=? AND status='pending'",
                    (plan_id, version)).fetchone()["s"]
                row = next((r for r in pending_rows if r["step"] == current), None)
                if row is None:
                    raise ConflictError(f"尚未轮到该方确认，当前待办步骤为 {current}")
                kind, ref = row["party_kind"], row["party_ref"]
                now = self._now_str()
                new_status = "confirmed" if approved else "rejected"
                conn.execute(
                    "UPDATE plan_confirmations SET status=?,decided_by=?,decided_at=?,comment=? "
                    "WHERE plan_id=? AND version=? AND party_kind=? AND party_ref=?",
                    (new_status, actor_id, now, comment.strip(),
                     plan_id, version, kind, ref))
                if not approved:
                    conn.execute("UPDATE tour_plans SET status=?, lease_expires_at=NULL "
                                 "WHERE plan_id=? AND version=?",
                                 (REJECTED, plan_id, version))
                    conn.execute(
                        "UPDATE resource_allocations SET status=?, lease_expires_at=NULL, released_at=? "
                        "WHERE plan_id=? AND version=? AND status=?",
                        (RELEASED, now, plan_id, version, HELD))
                    append_event(conn, actor_id=actor_id, action="tour.rejected",
                                 resource_type="tour_plan", resource_id=plan_id,
                                 detail={"version": version, "party_kind": kind, "party_ref": ref,
                                         "comment": comment}, occurred_at=now)
                    return "tour_confirmation", f"{plan_id}:{version}:{kind}:{ref}", {
                        "plan_id": plan_id, "version": version, "party_kind": kind,
                        "party_ref": ref, "status": REJECTED}
                # 确认成功：续租剩余草案资源
                lease = self._now() + timedelta(seconds=normalized["lease_seconds"])
                conn.execute(
                    "UPDATE resource_allocations SET lease_expires_at=? "
                    "WHERE plan_id=? AND version=? AND status=?",
                    (fmt(lease), plan_id, version, HELD))
                conn.execute(
                    "UPDATE tour_plans SET lease_expires_at=? WHERE plan_id=? AND version=?",
                    (fmt(lease), plan_id, version))
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM plan_confirmations WHERE plan_id=? AND version=? AND status='pending'",
                    (plan_id, version)).fetchone()["c"]
                append_event(conn, actor_id=actor_id, action="tour.confirmed",
                             resource_type="tour_plan", resource_id=plan_id,
                             detail={"version": version, "party_kind": kind, "party_ref": ref,
                                     "pending": pending, "lease_expires_at": fmt(lease)},
                             occurred_at=now)
                return "tour_confirmation", f"{plan_id}:{version}:{kind}:{ref}", {
                    "plan_id": plan_id, "version": version, "party_kind": kind,
                    "party_ref": ref, "status": "confirmed", "pending": pending,
                    "lease_expires_at": fmt(lease)}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="confirm_tour_plan",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _load_plan_version(self, conn, plan_id: str, version: int):
        plan = conn.execute("SELECT * FROM tour_plans WHERE plan_id=? AND version=?",
                            (plan_id, version)).fetchone()
        if plan is None:
            raise NotFoundError(f"计划 {plan_id} 版本 {version} 不存在")
        return plan

    # ---------------------------------------------------------------- 发布

    def publish(self, *, request_id: str, actor_id: str, plan_id: str, version: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "version": version}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以发布计划")
            self.sweep_expired(conn)
            plan = self._load_plan_version(conn, plan_id, version)

            def create():
                effective = conn.execute("SELECT * FROM plan_effective WHERE plan_id=?",
                                         (plan_id,)).fetchone()
                # 重复发布：幂等返回当前生效版本，不多占资源
                if effective and effective["version"] == version and plan["status"] == PUBLISHED:
                    return "tour_plan", plan_id, {"plan_id": plan_id, "version": version,
                                                  "status": PUBLISHED, "duplicate": True}
                if plan["status"] != DRAFT:
                    raise ConflictError(f"版本 {version} 状态为 {plan['status']}，不能发布")
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM plan_confirmations WHERE plan_id=? AND version=? AND status!='confirmed'",
                    (plan_id, version)).fetchone()["c"]
                if pending:
                    raise ConflictError("仍有责任方未完成确认，不能发布")
                if effective and version <= effective["version"]:
                    raise ConflictError(f"版本 {effective['version']} 已经生效，并发发布只允许更新版本生效")
                import json as _json
                normalized = self._rehydrate(_json.loads(plan["manifest_json"]))
                now = self._now_str()
                # 只有新版本能在此刻生效：立即事务 + 生效行互斥
                conn.execute(
                    "INSERT INTO plan_effective(plan_id,version,published_at) VALUES(?,?,?) "
                    "ON CONFLICT(plan_id) DO UPDATE SET version=excluded.version, published_at=excluded.published_at",
                    (plan_id, version, now))
                conn.execute(
                    "UPDATE tour_plans SET status=?, published_at=?, lease_expires_at=NULL "
                    "WHERE plan_id=? AND version=?",
                    (PUBLISHED, now, plan_id, version))
                conn.execute(
                    "UPDATE resource_allocations SET status=?, lease_expires_at=NULL "
                    "WHERE plan_id=? AND version=? AND status=?",
                    (CONFIRMED, plan_id, version, HELD))
                if effective:
                    conn.execute("UPDATE tour_plans SET status=? WHERE plan_id=? AND version=? AND status=?",
                                 (SUPERSEDED, plan_id, effective["version"], PUBLISHED))
                    self._release_superseded(conn, plan_id, effective["version"], version, now)
                self._publish_states(conn, plan_id, version, normalized, now)
                self._publish_costs(conn, plan_id, version, normalized, now, actor_id)
                self._publish_sessions(conn, plan_id, version, normalized, now, actor_id,
                                       parent_version=effective["version"] if effective else None)
                if effective:
                    conn.execute(
                        "UPDATE plan_incidents SET status='resolved', resolution_json=? "
                        "WHERE plan_id=? AND status='open'",
                        (canonical_json({"resolved_by_version": version}), plan_id))
                append_event(conn, actor_id=actor_id, action="tour.published",
                             resource_type="tour_plan", resource_id=plan_id,
                             detail={"version": version,
                                     "superseded_version": effective["version"] if effective else None,
                                     "publicity": [s.get("publicity") for s in normalized["stops"]
                                                   if s.get("publicity")]},
                             occurred_at=now)
                return "tour_plan", plan_id, {"plan_id": plan_id, "version": version,
                                              "status": PUBLISHED}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="publish_tour_plan",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _release_superseded(self, conn, plan_id: str, old_version: int, new_version: int,
                            now: str) -> None:
        """新版本完整重述计划：释放旧版本全部占用，账本由新版本唯一承接。

        已完成交接区段的承运资源仍出现在新版本清单中（重排只改受影响部分），
        因此不会丢失保护；交接事实本身记录在 segment_handovers，不受账本切换影响。
        """

        conn.execute(
            "UPDATE resource_allocations SET status=?, released_at=? "
            "WHERE plan_id=? AND version=? AND status=?",
            (RELEASED, now, plan_id, old_version, CONFIRMED))

    def _publish_states(self, conn, plan_id: str, version: int, normalized: dict[str, Any],
                        now: str) -> None:
        for stop in normalized["stops"]:
            existing = conn.execute("SELECT 1 FROM stop_states WHERE plan_id=? AND stop_key=?",
                                    (plan_id, stop["stop_key"])).fetchone()
            if existing:
                conn.execute(
                    "UPDATE stop_states SET current_version=?, updated_at=? WHERE plan_id=? AND stop_key=?",
                    (version, now, plan_id, stop["stop_key"]))
            else:
                conn.execute(
                    "INSERT INTO stop_states(plan_id,stop_key,state,current_version,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (plan_id, stop["stop_key"], "scheduled", version, now))
        for segment in normalized["segments"]:
            existing = conn.execute("SELECT * FROM segment_states WHERE plan_id=? AND segment_key=?",
                                    (plan_id, segment["segment_key"])).fetchone()
            if existing:
                # 在途/已交接状态不能被重排覆盖，只升级版本指针
                conn.execute(
                    "UPDATE segment_states SET current_version=?, updated_at=? WHERE plan_id=? AND segment_key=?",
                    (version, now, plan_id, segment["segment_key"]))
            else:
                conn.execute(
                    "INSERT INTO segment_states(plan_id,segment_key,state,current_version,detail_json,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, segment["segment_key"], "scheduled", version, "{}", now))

    def _cost_already_recorded(self, conn, plan_id: str, ref_kind: str,
                               resource_id: str) -> bool:
        """费用按具体资源记账：资源未变不重复计费，切换备用资源会追加新费用。"""

        return conn.execute(
            "SELECT 1 FROM plan_costs WHERE plan_id=? AND ref_kind=? AND ref_key=?",
            (plan_id, ref_kind, resource_id)).fetchone() is not None

    def _publish_costs(self, conn, plan_id: str, version: int, normalized: dict[str, Any],
                       now: str, actor_id: str) -> None:
        """费用只增不改：历史费用保留，仅为本版本新启用的资源追加费用。"""

        for stop in normalized["stops"]:
            window_id = stop["chosen"]["window_id"]
            if stop["chosen"]["cost"] <= 0 or self._cost_already_recorded(conn, plan_id, "stop", window_id):
                continue
            conn.execute(
                "INSERT INTO plan_costs(cost_id,plan_id,ref_kind,ref_key,amount,note,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, "stop", window_id,
                 stop["chosen"]["cost"], f"停点 {stop['stop_key']} 场馆/展柜/工时费用", actor_id, now))
        for segment in normalized["segments"]:
            slot_id = segment["chosen"]["slot_id"]
            if segment["chosen"]["cost"] <= 0 or self._cost_already_recorded(
                    conn, plan_id, "segment", slot_id):
                continue
            conn.execute(
                "INSERT INTO plan_costs(cost_id,plan_id,ref_kind,ref_key,amount,note,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, "segment", slot_id,
                 segment["chosen"]["cost"], f"区段 {segment['segment_key']} 运输费用", actor_id, now))
        for insurance in normalized["insurance"]:
            policy_id = insurance["policy_id"]
            if insurance["cost"] <= 0 or self._cost_already_recorded(
                    conn, plan_id, "insurance", policy_id):
                continue
            conn.execute(
                "INSERT INTO plan_costs(cost_id,plan_id,ref_kind,ref_key,amount,note,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, "insurance", policy_id,
                 insurance["cost"], "保险费用", actor_id, now))

    def _publish_sessions(self, conn, plan_id: str, version: int, normalized: dict[str, Any],
                          now: str, actor_id: str, parent_version: int | None) -> None:
        """公开排期变更落账：已公告场次的改期写入 session_changes，不做静默覆盖。"""

        old_times: dict[tuple[str, str], tuple[str, str]] = {}
        if parent_version is not None:
            import json as _json
            old_manifest = self._rehydrate(_json.loads(conn.execute(
                "SELECT manifest_json FROM tour_plans WHERE plan_id=? AND version=?",
                (plan_id, parent_version)).fetchone()["manifest_json"]))
            old_times = {(s["stop_key"], x["title"]): (x["starts_at"], x["ends_at"])
                         for s in old_manifest["stops"] for x in s["sessions"]}
        for stop in normalized["stops"]:
            for session in stop["sessions"]:
                title = session["title"]
                starts = session["starts_at"]
                ends = session["ends_at"]
                existing = conn.execute(
                    "SELECT * FROM audience_sessions WHERE plan_id=? AND stop_key=? AND title=?",
                    (plan_id, stop["stop_key"], title)).fetchone()
                if existing is None:
                    announced = 1 if session["announce"] else 0
                    conn.execute(
                        "INSERT INTO audience_sessions(session_id,plan_id,stop_key,title,starts_at,"
                        "ends_at,announced,announced_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, plan_id, stop["stop_key"], title, starts, ends,
                         announced, now if announced else None, "scheduled"))
                    continue
                changed = existing["starts_at"] != starts or existing["ends_at"] != ends
                if changed:
                    conn.execute(
                        "UPDATE audience_sessions SET starts_at=?, ends_at=?, status=? "
                        "WHERE session_id=?",
                        (starts, ends, "rescheduled", existing["session_id"]))
                    if existing["announced"]:
                        conn.execute(
                            "INSERT INTO session_changes(change_id,plan_id,version,session_id,"
                            "change_kind,old_starts_at,old_ends_at,new_starts_at,new_ends_at,"
                            "reason,notice_state,created_by,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, plan_id, version, existing["session_id"],
                             "reschedule", existing["starts_at"], existing["ends_at"],
                             starts, ends, "reroute", "noticed", actor_id, now))

    # --------------------------------------------------------------- 在途运行

    def dispatch_segment(self, *, request_id: str, actor_id: str, plan_id: str,
                         segment_key: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "segment_key": segment_key}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)

            def create():
                effective = self._require_effective(conn, plan_id)
                normalized = self._effective_manifest(conn, plan_id, effective["version"])
                segment = self._find_segment(normalized, segment_key)
                carrier = conn.execute("SELECT contact_actor_id FROM carriers WHERE carrier_id=?",
                                       (segment["chosen"]["carrier_id"],)).fetchone()
                state = conn.execute("SELECT * FROM segment_states WHERE plan_id=? AND segment_key=?",
                                     (plan_id, segment_key)).fetchone()
                if state is None:
                    raise NotFoundError("区段状态不存在")
                if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR) and actor.actor_id != carrier["contact_actor_id"]:
                    raise PermissionDenied("只有对应承运方可以发运该区段")
                if state["state"] != "scheduled":
                    raise ConflictError(f"区段当前状态 {state['state']}，不能重复发运")
                now = self._now_str()
                conn.execute(
                    "UPDATE segment_states SET state='in_transit', current_version=?, detail_json=?, updated_at=? "
                    "WHERE plan_id=? AND segment_key=?",
                    (effective["version"], canonical_json(
                        {"slot_id": segment["chosen"]["slot_id"], "dispatched_at": now}),
                     now, plan_id, segment_key))
                append_event(conn, actor_id=actor_id, action="segment.dispatched",
                             resource_type="tour_segment", resource_id=f"{plan_id}:{segment_key}",
                             detail={"version": effective["version"],
                                     "slot_id": segment["chosen"]["slot_id"]}, occurred_at=now)
                return "tour_segment", f"{plan_id}:{segment_key}", {
                    "plan_id": plan_id, "segment_key": segment_key, "state": "in_transit"}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="dispatch_segment",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def complete_handover(self, *, request_id: str, actor_id: str, plan_id: str,
                          segment_key: str, condition_note: str) -> dict[str, Any]:
        """合规接收人完成交接；交接记录此后不可修改、不可静默覆盖。"""

        condition_note = _require_str(condition_note, "condition_note", 500)
        payload = {"actor_id": actor_id, "plan_id": plan_id, "segment_key": segment_key,
                   "condition_note": condition_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)

            def create():
                effective = self._require_effective(conn, plan_id)
                normalized = self._effective_manifest(conn, plan_id, effective["version"])
                segment = self._find_segment(normalized, segment_key)
                stop = self._find_stop(normalized, segment["to_stop_key"])
                receiver = conn.execute(
                    "SELECT * FROM receivers WHERE receiver_id=? AND active=1",
                    (stop["chosen"]["receiver_id"],)).fetchone()
                if receiver is None:
                    raise NotFoundError("接收人已失效，无法完成合规交接")
                if actor.actor_id != receiver["actor_id"] and actor.role not in (ROLE_ADMIN,):
                    raise PermissionDenied("只有该场馆登记的合规接收人可以完成交接")
                state = conn.execute("SELECT * FROM segment_states WHERE plan_id=? AND segment_key=?",
                                     (plan_id, segment_key)).fetchone()
                if state["state"] != "in_transit":
                    raise ConflictError(f"区段状态 {state['state']}，不能交接")
                now_dt = self._now()
                now = fmt(now_dt)
                handover_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO segment_handovers(handover_id,plan_id,segment_key,receiver_id,"
                    "condition_note,completed_by,completed_at) VALUES(?,?,?,?,?,?,?)",
                    (handover_id, plan_id, segment_key, receiver["receiver_id"],
                     condition_note, actor.actor_id, now))
                conn.execute(
                    "UPDATE segment_states SET state='delivered', updated_at=? "
                    "WHERE plan_id=? AND segment_key=?",
                    (now, plan_id, segment_key))
                conn.execute(
                    "UPDATE stop_states SET state='ready', handover_completed_at=?, updated_at=? "
                    "WHERE plan_id=? AND stop_key=?",
                    (now, now, plan_id, stop["stop_key"]))
                append_event(conn, actor_id=actor_id, action="segment.handover_completed",
                             resource_type="tour_segment", resource_id=f"{plan_id}:{segment_key}",
                             detail={"version": effective["version"], "receiver_id": receiver["receiver_id"],
                                     "qualification": receiver["qualification"],
                                     "condition_note": condition_note}, occurred_at=now)
                return "tour_handover", handover_id, {"handover_id": handover_id,
                                                      "plan_id": plan_id,
                                                      "segment_key": segment_key,
                                                      "state": "delivered"}

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="complete_handover",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ------------------------------------------------------------- 事故与重排

    def report_incident(self, *, request_id: str, actor_id: str, plan_id: str, kind: str,
                        stop_keys: list[str] | None = None, segment_keys: list[str] | None = None,
                        detail: dict[str, Any] | None = None,
                        notice_confirmed: bool = False) -> dict[str, Any]:
        """上报运输延误或作品损伤；窗口缩短/闭馆由场馆入口自动触发。"""

        allowed = {"transport_delay", "artwork_damaged", "window_shortened", "venue_closed"}
        if kind not in allowed:
            raise ValidationError(f"事故类型必须是 {sorted(allowed)} 之一")
        detail = detail or {}
        payload = {"actor_id": actor_id, "plan_id": plan_id, "kind": kind,
                   "stop_keys": stop_keys or [], "segment_keys": segment_keys or [],
                   "detail": detail, "notice_confirmed": notice_confirmed}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role == ROLE_AUDITOR:
                raise PermissionDenied("审计员不能上报业务事故")
            self._require_effective(conn, plan_id)

            def create():
                reroute = self._record_incident_and_reroute(
                    conn, actor_id=actor_id, plan_id=plan_id, kind=kind,
                    stop_keys=stop_keys or [], segment_keys=segment_keys or [],
                    detail=detail, notice_confirmed=notice_confirmed)
                return "tour_incident", reroute["incident_id"], reroute

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="report_incident",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _has_open_incident(self, conn, plan_id: str, kind: str, resource_id: str) -> bool:
        for row in conn.execute(
                "SELECT scope_json, detail_json FROM plan_incidents WHERE plan_id=? AND status='open'",
                (plan_id,)).fetchall():
            import json as _json
            if _json.loads(row["detail_json"]).get("window_id") == resource_id \
                    or _json.loads(row["detail_json"]).get("venue_id") == resource_id:
                return True
        return False

    def _plans_using_window(self, conn, plan_id: str, window_id: str) -> list[str]:
        effective = conn.execute("SELECT version FROM plan_effective WHERE plan_id=?",
                                 (plan_id,)).fetchone()
        if not effective:
            return []
        rows = conn.execute(
            "SELECT stop_key, payload_json FROM plan_stops_snapshot WHERE plan_id=? AND version=?",
            (plan_id, effective["version"])).fetchall()
        import json as _json
        affected = []
        for row in rows:
            payload = _json.loads(row["payload_json"])
            if payload["chosen"]["window_id"] == window_id:
                affected.append(row["stop_key"])
        return affected

    def _record_reroute_failure(self, conn, plan_id: str, incident_id: str | None,
                                exc: ConflictError, now: str) -> None:
        """重排无法自动完成时留痕，供运营查询冲突原因与恢复路径。"""

        effective = conn.execute("SELECT version FROM plan_effective WHERE plan_id=?",
                                 (plan_id,)).fetchone()
        version = effective["version"] if effective else 0
        conn.execute(
            "INSERT INTO plan_conflicts(plan_id,version,detail_json,created_at) VALUES(?,?,?,?)",
            (plan_id, version, canonical_json(
                {"kind": "reroute_unavailable", "incident_id": incident_id,
                 "reason": str(exc),
                 "recovery": ["由运营人员通过 replan 指定其他备用资源",
                              "等待被占用资源的限时租约到期",
                              "确认观众告知（notice_confirmed=true）后重新申报"]}),
             now))
        append_event(conn, actor_id="system", action="tour.reroute_unavailable",
                     resource_type="tour_plan", resource_id=plan_id,
                     detail={"incident_id": incident_id, "reason": str(exc)},
                     occurred_at=now)

    def _record_incident_and_reroute(self, conn, *, actor_id: str, plan_id: str, kind: str,
                                     stop_keys: list[str], segment_keys: list[str],
                                     detail: dict[str, Any], notice_confirmed: bool) -> dict[str, Any]:
        effective = conn.execute("SELECT * FROM plan_effective WHERE plan_id=?",
                                 (plan_id,)).fetchone()
        if effective is None:
            raise NotFoundError(f"计划 {plan_id} 尚未发布")
        now = self._now_str()
        incident_id = uuid.uuid4().hex
        scope = {"stop_keys": stop_keys, "segment_keys": segment_keys}
        conn.execute(
            "INSERT INTO plan_incidents(incident_id,plan_id,version,kind,scope_json,detail_json,"
            "reported_by,created_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (incident_id, plan_id, effective["version"], kind, canonical_json(scope),
             canonical_json(detail), actor_id, now, "open"))
        for key in stop_keys:
            conn.execute("UPDATE stop_states SET state='disrupted', updated_at=? "
                         "WHERE plan_id=? AND stop_key=?", (now, plan_id, key))
        for key in segment_keys:
            conn.execute(
                "UPDATE segment_states SET state='disrupted', updated_at=? "
                "WHERE plan_id=? AND segment_key=? AND state!='delivered'",
                (now, plan_id, key))
        append_event(conn, actor_id=actor_id, action="tour.incident_reported",
                     resource_type="tour_incident", resource_id=incident_id,
                     detail={"plan_id": plan_id, "kind": kind, "scope": scope, "detail": detail},
                     occurred_at=now)
        return self._reroute(
            conn, actor_id=actor_id, plan_id=plan_id, kind=kind,
            changed_stops=set(stop_keys), changed_segments=set(segment_keys),
            detail=detail, notice_confirmed=notice_confirmed, incident_id=incident_id)

    def replan(self, *, request_id: str, actor_id: str, plan_id: str,
               stop_overrides: dict[str, int] | None = None,
               segment_overrides: dict[str, dict[str, Any]] | None = None,
               notice_confirmed: bool = False) -> dict[str, Any]:
        """运营人员手动重排：为指定停点/区段切换备用选项（按稳定优先级）。"""

        stop_overrides = stop_overrides or {}
        segment_overrides = segment_overrides or {}
        payload = {"actor_id": actor_id, "plan_id": plan_id,
                   "stop_overrides": stop_overrides, "segment_overrides": segment_overrides,
                   "notice_confirmed": notice_confirmed}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role not in (ROLE_ADMIN, ROLE_OPERATOR):
                raise PermissionDenied("只有运营人员可以发起重排")

            def create():
                reroute = self._reroute(
                    conn, actor_id=actor_id, plan_id=plan_id, kind="manual_replan",
                    changed_stops=set(stop_overrides), changed_segments=set(segment_overrides),
                    detail={"stop_overrides": stop_overrides, "segment_overrides": segment_overrides},
                    notice_confirmed=notice_confirmed, incident_id=None,
                    stop_overrides=stop_overrides, segment_overrides=segment_overrides)
                return "tour_plan", plan_id, reroute

            _, _, response, replayed = self._idempotent(
                conn, request_id=request_id, action="replan_tour",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _reroute(self, conn, *, actor_id: str, plan_id: str, kind: str,
                 changed_stops: set[str], changed_segments: set[str],
                 detail: dict[str, Any], notice_confirmed: bool,
                 incident_id: str | None,
                 stop_overrides: dict[str, int] | None = None,
                 segment_overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """只重排受影响区段，生成下一草案版本；不动已交接区段与历史费用。"""

        effective = conn.execute("SELECT * FROM plan_effective WHERE plan_id=?",
                                 (plan_id,)).fetchone()
        if effective is None:
            raise NotFoundError(f"计划 {plan_id} 尚未发布，不能重排")
        if conn.execute("SELECT 1 FROM tour_plans WHERE plan_id=? AND status=?",
                        (plan_id, DRAFT)).fetchone():
            raise ConflictError("该计划已有待确认的重排草案")
        import json as _json
        base = self._rehydrate(_json.loads(conn.execute(
            "SELECT manifest_json FROM tour_plans WHERE plan_id=? AND version=?",
            (plan_id, effective["version"])).fetchone()["manifest_json"]))

        # 已完成交接的区段不允许重排
        for key in changed_segments:
            state = conn.execute("SELECT state FROM segment_states WHERE plan_id=? AND segment_key=?",
                                 (plan_id, key)).fetchone()
            if state and state["state"] == "delivered":
                raise ConflictError(f"区段 {key} 已完成交接，不能重排")

        attempts: list[dict[str, Any]] = []
        chosen_indices: dict[str, int] = {}
        segment_times: dict[str, dict[str, str]] = {}
        # 事故自动选择：按 options 稳定优先级逐个尝试
        stop_keys = changed_stops or set()
        seg_keys = changed_segments or set()
        for stop in base["stops"]:
            if stop["stop_key"] in stop_keys:
                override = (stop_overrides or {}).get(stop["stop_key"])
                chosen_indices[stop["stop_key"]] = self._pick_stop_option(
                    conn, base, stop, override=override, attempts=attempts)
        for segment in base["segments"]:
            if segment["segment_key"] in seg_keys:
                override = (segment_overrides or {}).get(segment["segment_key"])
                picked_index, times = self._pick_segment_option(
                    conn, base, segment, detail if kind == "transport_delay" else None,
                    override=override, attempts=attempts)
                chosen_indices[segment["segment_key"]] = picked_index
                if times:
                    segment_times[segment["segment_key"]] = times

        # 运输延误可能顺延下游停点
        delay_shift: timedelta | None = None
        if kind == "transport_delay" and segment_times:
            key = next(iter(segment_times))
            segment = self._find_segment(base, key)
            old_deliver = segment["chosen"]["deliver_at_dt"]
            new_deliver = parse_ts(segment_times[key]["deliver_at"], "delay.deliver_at")
            if new_deliver > old_deliver:
                delay_shift = new_deliver - old_deliver

        candidate, affected_stops = self._candidate_manifest(
            conn, base, chosen_indices, segment_times, delay_shift, changed_stops, changed_segments)

        # 公开排期保护：检查受影响的已公告场次
        session_impact = self._session_impact(conn, plan_id, candidate, affected_stops)
        announced_changes = [item for item in session_impact if item["announced"]]
        if announced_changes and not notice_confirmed:
            raise ConflictError(
                "重排将改变已公开排期，必须显式确认观众告知（notice_confirmed=true）："
                + canonical_json([{"session": x["title"], "change": x["change_kind"]}
                                  for x in announced_changes]))

        conflicts = self._detect_conflicts(conn, candidate, plan_id)
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM tour_plans WHERE plan_id=?",
                               (plan_id,)).fetchone()["v"]
        lease = self._now() + timedelta(seconds=candidate["lease_seconds"])
        status = BLOCKED if conflicts else DRAFT
        conn.execute(
            "INSERT INTO tour_plans(plan_id,version,status,rep_actor_id,manifest_json,parent_version,"
            "content_hash,lease_expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plan_id, version, status, candidate["rep_actor_id"],
             canonical_json(strip_datetimes(candidate)),
             effective["version"], digest(strip_datetimes(candidate)),
             fmt(lease) if status == DRAFT else None, actor_id, self._now_str()))
        self._write_snapshots(conn, plan_id, version, candidate)
        self._write_allocations(conn, plan_id, version, candidate,
                                lease if status == DRAFT else None, blocked=bool(conflicts))
        if conflicts:
            conn.execute(
                "INSERT INTO plan_conflicts(plan_id,version,detail_json,created_at) VALUES(?,?,?,?)",
                (plan_id, version, canonical_json(
                    {"kind": "reroute_conflict", "incident_id": incident_id, "items": conflicts,
                     "attempts": attempts,
                     "recovery": ["继续按 options 优先级尝试后续备用资源",
                                  "由运营人员通过 replan 指定其他选项"]}), self._now_str()))
        else:
            self._build_confirmations(conn, plan_id, version, candidate,
                                      changed_stops=affected_stops,
                                      changed_segments=set(changed_segments))
        append_event(conn, actor_id=actor_id,
                     action="tour.reroute_drafted" if not conflicts else "tour.reroute_blocked",
                     resource_type="tour_plan", resource_id=plan_id,
                     detail={"version": version, "kind": kind, "incident_id": incident_id,
                             "changed_stops": sorted(affected_stops),
                             "changed_segments": sorted(changed_segments),
                             "attempts": attempts, "conflicts": len(conflicts),
                             "session_impact": session_impact},
                     occurred_at=self._now_str())
        return {"plan_id": plan_id, "version": version, "status": status,
                "incident_id": incident_id, "kind": kind, "attempts": attempts,
                "changed_stops": sorted(affected_stops),
                "changed_segments": sorted(changed_segments),
                "session_impact": session_impact,
                "lease_expires_at": fmt(lease) if status == DRAFT else None,
                "conflicts": conflicts}

    def _pick_stop_option(self, conn, base: dict[str, Any], stop: dict[str, Any],
                          *, override: int | None, attempts: list[dict[str, Any]]) -> int:
        window_cache: dict[str, str] = {}

        def window_status(venue_id, window_id):
            key = f"{venue_id}:{window_id}"
            if key not in window_cache:
                row = conn.execute("SELECT status FROM venue_windows WHERE window_id=?",
                                   (window_id,)).fetchone()
                window_cache[key] = row["status"] if row else "missing"
            return window_cache[key]

        indices = [override] if override is not None else range(0, len(stop["options"]))
        last_blocker = None
        for index in indices:
            if not isinstance(index, int) or index < 0 or index >= len(stop["options"]):
                raise ValidationError(f"停点 {stop['stop_key']} 的备用选项下标越界")
            option = stop["options"][index]
            status = window_status(option["venue_id"], option["window_id"])
            blockers = []
            if status != "open":
                blockers.append(f"window_{status}")
            if self._option_resource_busy(conn, "venue_window", option["window_id"],
                                          option["install_at"], option["dismantle_at"], base["plan_id"]):
                blockers.append("window_double_booked")
            if self._option_resource_busy(conn, "display_case", option["case_id"],
                                          option["install_at"], option["dismantle_at"], base["plan_id"]):
                blockers.append("case_double_booked")
            if self._option_resource_busy(conn, "venue_labor", option["labor_id"],
                                          option["install_at"], option["dismantle_at"], base["plan_id"]):
                blockers.append("labor_double_booked")
            attempt = {"stop_key": stop["stop_key"], "option_index": index,
                       "venue_id": option["venue_id"], "blockers": blockers}
            attempts.append(attempt)
            if not blockers:
                return index
            last_blocker = blockers
        raise ConflictError(
            f"停点 {stop['stop_key']} 的全部备用场馆都不可用：{canonical_json(last_blocker)}")

    def _pick_segment_option(self, conn, base: dict[str, Any], segment: dict[str, Any],
                             delay_detail: dict[str, Any] | None, *,
                             override: dict[str, Any] | None,
                             attempts: list[dict[str, Any]]) -> tuple[int, dict[str, str] | None]:
        indices = [override["option_index"]] if override else range(0, len(segment["options"]))
        wanted_times = None
        if delay_detail:
            wanted_times = {
                "pickup_at": delay_detail.get("new_pickup_at") or segment["chosen"]["pickup_at"],
                "deliver_at": delay_detail.get("new_deliver_at") or segment["chosen"]["deliver_at"],
            }
        elif override and override.get("pickup_at"):
            wanted_times = {"pickup_at": override["pickup_at"], "deliver_at": override["deliver_at"]}
        last_blocker = None
        for index in indices:
            if not isinstance(index, int) or index < 0 or index >= len(segment["options"]):
                raise ValidationError(f"区段 {segment['segment_key']} 的备用选项下标越界")
            option = dict(segment["options"][index])
            blockers = []
            slot = conn.execute("SELECT status,starts_at,ends_at FROM carrier_slots WHERE slot_id=?",
                                (option["slot_id"],)).fetchone()
            if slot is None or slot["status"] != "open":
                blockers.append("slot_unavailable")
            pickup = parse_ts(option["pickup_at"], "option.pickup_at")
            deliver = parse_ts(option["deliver_at"], "option.deliver_at")
            if wanted_times:
                pickup = parse_ts(wanted_times["pickup_at"], "new_pickup_at")
                deliver = parse_ts(wanted_times["deliver_at"], "new_deliver_at")
                if parse_ts(slot["starts_at"], "slot.starts_at") > pickup or \
                        parse_ts(slot["ends_at"], "slot.ends_at") < deliver:
                    blockers.append("slot_time_mismatch")
            if self._option_resource_busy(conn, "carrier_slot", option["slot_id"],
                                          fmt(pickup), fmt(deliver), base["plan_id"]):
                blockers.append("slot_double_booked")
            attempt = {"segment_key": segment["segment_key"], "option_index": index,
                       "carrier_id": option["carrier_id"], "blockers": blockers}
            attempts.append(attempt)
            if not blockers:
                times = {"pickup_at": fmt(pickup), "deliver_at": fmt(deliver)} if wanted_times else None
                return index, times
            last_blocker = blockers
        raise ConflictError(
            f"区段 {segment['segment_key']} 的全部备用承运方案都不可用：{canonical_json(last_blocker)}")

    @staticmethod
    def _option_resource_busy(conn, resource_type: str, resource_id: str,
                              starts_at: str, ends_at: str, plan_id: str) -> bool:
        rows = conn.execute(
            "SELECT starts_at,ends_at FROM resource_allocations WHERE resource_type=? AND resource_id=? "
            "AND status IN (?,?) AND plan_id!=?",
            (resource_type, resource_id, HELD, CONFIRMED, plan_id)).fetchall()
        return any(TourService._overlaps(r["starts_at"], r["ends_at"], starts_at, ends_at) for r in rows)

    def _candidate_manifest(self, conn, base: dict[str, Any], chosen_indices: dict[str, int],
                            segment_times: dict[str, dict[str, str]], delay_shift: timedelta | None,
                            changed_stops: set[str], changed_segments: set[str]
                            ) -> tuple[dict[str, Any], set[str]]:
        """基于生效清单生成候选清单并返回（候选, 受影响停点集合）。"""

        import copy
        candidate = copy.deepcopy(base)
        for key, index in chosen_indices.items():
            for stop in candidate["stops"]:
                if stop["stop_key"] == key:
                    stop["chosen_index"] = index
            for segment in candidate["segments"]:
                if segment["segment_key"] == key:
                    segment["chosen_index"] = index
                    if key in segment_times:
                        option = segment["options"][index]
                        option["pickup_at"] = segment_times[key]["pickup_at"]
                        option["deliver_at"] = segment_times[key]["deliver_at"]

        affected_stops = set(changed_stops)
        # 延误顺延：到达停点及其后所有停点整体平移，观众场次同步平移
        if delay_shift is not None and changed_segments:
            first_changed = next(iter(changed_segments))
            order = [s["stop_key"] for s in candidate["stops"]]
            target_key = self._find_segment(candidate, first_changed)["to_stop_key"]
            target_index = order.index(target_key)
            for stop in candidate["stops"][target_index:]:
                affected_stops.add(stop["stop_key"])
                chosen = stop["options"][stop["chosen_index"]]
                window = conn.execute("SELECT * FROM venue_windows WHERE window_id=?",
                                      (chosen["window_id"],)).fetchone()
                for field in ("install_at", "open_at", "close_at", "dismantle_at"):
                    chosen[field] = fmt(parse_ts(chosen[field], field) + delay_shift)
                window_ok = (parse_ts(window["starts_at"], "window.starts_at")
                             <= parse_ts(chosen["install_at"], "install_at")
                             and parse_ts(chosen["dismantle_at"], "dismantle_at")
                             <= parse_ts(window["ends_at"], "window.ends_at"))
                if not window_ok:
                    raise ConflictError(
                        f"延误顺延导致停点 {stop['stop_key']} 超出场馆窗口，必须启用备用场馆")
                for session in stop["sessions"]:
                    session["starts_at"] = fmt(parse_ts(session["starts_at"], "starts_at") + delay_shift)
                    session["ends_at"] = fmt(parse_ts(session["ends_at"], "ends_at") + delay_shift)
        normalized = self._normalize_manifest(conn, strip_datetimes(candidate))
        return normalized, affected_stops

    def _downstream_stops(self, base: dict[str, Any], segment_key: str) -> set[str]:
        order = [s["stop_key"] for s in base["stops"]]
        target = self._find_segment(base, segment_key)["to_stop_key"]
        index = order.index(target)
        return set(order[index:])

    def _session_impact(self, conn, plan_id: str, candidate: dict[str, Any],
                        affected_stops: set[str]) -> list[dict[str, Any]]:
        """对比生效版本与候选版本，列出受影响的观众场次。"""

        impact = []
        old = self._sessions_map(conn, plan_id)
        for stop in candidate["stops"]:
            if stop["stop_key"] not in affected_stops:
                continue
            for session in stop["sessions"]:
                previous = old.get((stop["stop_key"], session["title"]))
                if previous is None:
                    continue
                announced = bool(previous["announced"])
                if previous["starts_at"] != session["starts_at"] or previous["ends_at"] != session["ends_at"]:
                    impact.append({
                        "stop_key": stop["stop_key"], "session_id": previous["session_id"],
                        "title": session["title"], "announced": announced,
                        "change_kind": "reschedule",
                        "old_starts_at": previous["starts_at"], "old_ends_at": previous["ends_at"],
                        "new_starts_at": session["starts_at"], "new_ends_at": session["ends_at"],
                    })
        return impact

    def _sessions_map(self, conn, plan_id: str) -> dict[tuple[str, str], Any]:
        result = {}
        for row in conn.execute(
                "SELECT * FROM audience_sessions WHERE plan_id=?", (plan_id,)).fetchall():
            result[(row["stop_key"], row["title"])] = row
        return result

    def _find_segment(self, normalized: dict[str, Any], segment_key: str) -> dict[str, Any]:
        for segment in normalized["segments"]:
            if segment["segment_key"] == segment_key:
                return segment
        raise NotFoundError(f"区段 {segment_key} 不存在")

    def _find_stop(self, normalized: dict[str, Any], stop_key: str) -> dict[str, Any]:
        for stop in normalized["stops"]:
            if stop["stop_key"] == stop_key:
                return stop
        raise NotFoundError(f"停点 {stop_key} 不存在")

    def _require_effective(self, conn, plan_id: str):
        effective = conn.execute("SELECT * FROM plan_effective WHERE plan_id=?",
                                 (plan_id,)).fetchone()
        if effective is None:
            raise NotFoundError(f"计划 {plan_id} 尚未发布")
        return effective

    def _effective_manifest(self, conn, plan_id: str, version: int) -> dict[str, Any]:
        import json as _json
        return self._rehydrate(_json.loads(conn.execute(
            "SELECT manifest_json FROM tour_plans WHERE plan_id=? AND version=?",
            (plan_id, version)).fetchone()["manifest_json"]))

    # ----------------------------------------------------------------- 查询

    def list_todos(self, actor_id: str) -> list[dict[str, Any]]:
        """返回某参与者的待办及最小必要资料。"""

        with self.database.transaction(immediate=True) as conn:
            self.sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            todos: list[dict[str, Any]] = []
            draft_rows = conn.execute("SELECT * FROM tour_plans WHERE status=?", (DRAFT,)).fetchall()
            import json as _json
            for plan in draft_rows:
                normalized = self._rehydrate(_json.loads(plan["manifest_json"]))
                current = conn.execute(
                    "SELECT MIN(step) AS s FROM plan_confirmations WHERE plan_id=? AND version=? AND status='pending'",
                    (plan["plan_id"], plan["version"])).fetchone()["s"]
                if current is None:
                    continue
                step_row = conn.execute(
                    "SELECT * FROM plan_confirmations WHERE plan_id=? AND version=? AND step=?",
                    (plan["plan_id"], plan["version"], current)).fetchone()
                bindings = self._parties_for_actor(conn, normalized, actor_id)
                if (step_row["party_kind"], step_row["party_ref"]) not in bindings:
                    continue
                kind, ref = step_row["party_kind"], step_row["party_ref"]
                todos.append({
                    "kind": "confirmation",
                    "plan_id": plan["plan_id"],
                    "version": plan["version"],
                    "step": current,
                    "party_kind": kind,
                    "party_ref": ref,
                    "lease_expires_at": plan["lease_expires_at"],
                    "materials": self._minimal_materials(conn, normalized, kind, ref),
                    "instruction": step_row["instruction"],
                })
            # 运营人员：阻塞草案与开放事故
            if actor.role in (ROLE_ADMIN, ROLE_OPERATOR):
                for row in conn.execute(
                        "SELECT p.* FROM tour_plans p WHERE p.status=?",
                        (BLOCKED,)).fetchall():
                    conflict = conn.execute(
                        "SELECT detail_json FROM plan_conflicts WHERE plan_id=? AND version=?",
                        (row["plan_id"], row["version"])).fetchone()
                    todos.append({"kind": "blocked_plan", "plan_id": row["plan_id"],
                                  "version": row["version"],
                                  "materials": _json.loads(conflict["detail_json"]) if conflict else {}})
                for row in conn.execute("SELECT * FROM plan_incidents WHERE status='open'").fetchall():
                    todos.append({"kind": "open_incident", "incident_id": row["incident_id"],
                                  "plan_id": row["plan_id"], "version": row["version"],
                                  "materials": {"kind": row["kind"],
                                                "scope": _json.loads(row["scope_json"]),
                                                "detail": _json.loads(row["detail_json"])}})
            # 接收人：到货物待交接
            receiver_rows = conn.execute(
                "SELECT receiver_id,venue_id,qualification FROM receivers WHERE actor_id=?",
                (actor_id,)).fetchall()
            receiver_ids = {r["receiver_id"] for r in receiver_rows}
            for effective in conn.execute("SELECT * FROM plan_effective").fetchall():
                states = conn.execute(
                    "SELECT segment_key FROM segment_states WHERE plan_id=? AND state='in_transit'",
                    (effective["plan_id"],)).fetchall()
                if not states:
                    continue
                normalized = self._effective_manifest(conn, effective["plan_id"], effective["version"])
                for state in states:
                    segment = self._find_segment(normalized, state["segment_key"])
                    stop = self._find_stop(normalized, segment["to_stop_key"])
                    if stop["chosen"]["receiver_id"] in receiver_ids:
                        todos.append({
                            "kind": "handover",
                            "plan_id": effective["plan_id"],
                            "segment_key": state["segment_key"],
                            "materials": {
                                "stop_key": stop["stop_key"],
                                "venue_id": stop["chosen"]["venue_id"],
                                "deliver_at": segment["chosen"]["deliver_at"],
                                "required_qualification": stop["chosen"]["qualification"],
                                "artwork_count": len(normalized["artwork_ids"]),
                            }})
            return todos

    def _minimal_materials(self, conn, normalized: dict[str, Any], kind: str,
                           ref: str) -> dict[str, Any]:
        """按参与方职责裁剪资料，各方只看到与自己这一步相关的最小集合。"""

        if kind == PARTY_REPRESENTATIVE:
            return {
                "artworks": [{"artwork_id": aid, "title": item["title"], "spec": item["spec"]}
                             for aid, item in sorted(normalized["artworks"].items())],
                "stops": [{"stop_key": s["stop_key"], "venue_id": s["chosen"]["venue_id"],
                           "open_at": s["chosen"]["open_at"], "close_at": s["chosen"]["close_at"],
                           "sessions": [{"title": x["title"], "starts_at": x["starts_at"],
                                         "ends_at": x["ends_at"]} for x in s["sessions"]]}
                          for s in normalized["stops"]],
                "segments": [{"segment_key": s["segment_key"],
                              "carrier_id": s["chosen"]["carrier_id"],
                              "pickup_at": s["chosen"]["pickup_at"],
                              "deliver_at": s["chosen"]["deliver_at"]}
                             for s in normalized["segments"]],
            }
        if kind == PARTY_INSURANCE:
            assignment = next((item for item in normalized["insurance"]
                               if item["policy_id"] == ref), None)
            covered = set(assignment["artwork_ids"]) if assignment else set()
            return {
                "policy_id": ref,
                "artworks": [{"artwork_id": aid, "title": item["title"],
                              "declared_value": item["declared_value"]}
                             for aid, item in sorted(normalized["artworks"].items())
                             if aid in covered],
                "insured_amount": assignment["amount"] if assignment else None,
                "cover_from": assignment["cover_from"] if assignment else None,
                "cover_to": assignment["cover_to"] if assignment else None,
                "transport_window": {
                    "from": min(s["chosen"]["pickup_at"] for s in normalized["segments"]),
                    "to": max(s["chosen"]["deliver_at"] for s in normalized["segments"]),
                }}
        if kind == PARTY_VENUE:
            stops = []
            for stop in normalized["stops"]:
                if stop["chosen"]["venue_id"] != ref:
                    continue
                stops.append({
                    "stop_key": stop["stop_key"],
                    "install_at": stop["chosen"]["install_at"],
                    "open_at": stop["chosen"]["open_at"],
                    "close_at": stop["chosen"]["close_at"],
                    "dismantle_at": stop["chosen"]["dismantle_at"],
                    "case_id": stop["chosen"]["case_id"],
                    "labor_id": stop["chosen"]["labor_id"],
                    "required_qualification": stop["chosen"]["qualification"],
                    "handling": [{"artwork_id": aid, "spec": item["spec"]}
                                 for aid, item in sorted(normalized["artworks"].items())],
                    "sessions": [{"title": x["title"], "starts_at": x["starts_at"],
                                  "ends_at": x["ends_at"]} for x in stop["sessions"]],
                })
            return {"venue_id": ref, "stops": stops}
        # 承运方：只返回本方承担的区段
        return {"carrier_id": ref, "segments": [{
            "segment_key": s["segment_key"],
            "pickup_at": s["chosen"]["pickup_at"],
            "deliver_at": s["chosen"]["deliver_at"],
            "load": {"weight_kg": s["chosen"]["total_weight_kg"],
                     "volume_m3": s["chosen"]["total_volume_m3"],
                     "pieces": s["chosen"]["total_pieces"]},
            "to_venue_id": next(stop["chosen"]["venue_id"] for stop in normalized["stops"]
                                if stop["stop_key"] == s["to_stop_key"]),
        } for s in normalized["segments"] if s["chosen"]["carrier_id"] == ref]}

    def get_plan(self, plan_id: str, *, actor_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as conn:
            if actor_id is not None:
                self._actor(conn, actor_id)
            versions = conn.execute("SELECT * FROM tour_plans WHERE plan_id=? ORDER BY version",
                                    (plan_id,)).fetchall()
            if not versions:
                raise NotFoundError("计划不存在")
            import json as _json
            effective = conn.execute("SELECT * FROM plan_effective WHERE plan_id=?",
                                     (plan_id,)).fetchone()
            return {
                "plan_id": plan_id,
                "effective_version": effective["version"] if effective else None,
                "versions": [{"version": r["version"], "status": r["status"],
                              "parent_version": r["parent_version"],
                              "created_at": r["created_at"],
                              "published_at": r["published_at"],
                              "lease_expires_at": r["lease_expires_at"],
                              "content_hash": r["content_hash"]} for r in versions],
                "stop_states": [dict(r) for r in conn.execute(
                    "SELECT * FROM stop_states WHERE plan_id=?", (plan_id,)).fetchall()],
                "segment_states": [dict(r) for r in conn.execute(
                    "SELECT * FROM segment_states WHERE plan_id=?", (plan_id,)).fetchall()],
            }

    def _query_open(self, conn, actor_id: str | None) -> Actor:
        if actor_id is None:
            return None  # type: ignore[return-value]
        return self._actor(conn, actor_id)

    def get_conflicts(self, plan_id: str, *, actor_id: str | None = None) -> dict[str, Any]:
        """查询每个阻塞版本的冲突原因与恢复路径。"""

        with self.database.transaction() as conn:
            self._query_open(conn, actor_id)
            rows = conn.execute(
                "SELECT * FROM plan_conflicts WHERE plan_id=? ORDER BY version DESC",
                (plan_id,)).fetchall()
            if not rows:
                raise NotFoundError("该计划没有阻塞记录")
            import json as _json
            return {"plan_id": plan_id, "items": [
                {"version": r["version"], "created_at": r["created_at"],
                 "detail": _json.loads(r["detail_json"])} for r in rows]}

    def get_session_changes(self, plan_id: str, *, actor_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as conn:
            self._query_open(conn, actor_id)
            rows = conn.execute(
                "SELECT * FROM session_changes WHERE plan_id=? ORDER BY created_at",
                (plan_id,)).fetchall()
            return {"plan_id": plan_id, "items": [dict(r) for r in rows]}

    def get_costs(self, plan_id: str, *, actor_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as conn:
            self._query_open(conn, actor_id)
            rows = conn.execute("SELECT * FROM plan_costs WHERE plan_id=? ORDER BY created_at",
                                (plan_id,)).fetchall()
            return {"plan_id": plan_id, "total": sum(r["amount"] for r in rows),
                    "items": [dict(r) for r in rows]}

    def restore_at(self, plan_id: str, at: str, *, actor_id: str | None = None) -> dict[str, Any]:
        """审计员按历史时点还原责任链与当时的生效版本。"""

        point = parse_ts(at, "at")
        point_str = fmt(point)
        with self.database.transaction() as conn:
            if actor_id is not None:
                actor = self._actor(conn, actor_id)
                if actor.role not in (ROLE_AUDITOR, ROLE_ADMIN, ROLE_OPERATOR):
                    raise PermissionDenied("只有审计/运营人员可以还原历史时点")
            versions = conn.execute("SELECT * FROM tour_plans WHERE plan_id=? ORDER BY version",
                                    (plan_id,)).fetchall()
            if not versions:
                raise NotFoundError("计划不存在")
            chain = []
            effective_at = None
            candidate_rows = conn.execute(
                "SELECT * FROM audit_events WHERE resource_type IN "
                "('tour_plan','tour_segment','tour_incident') AND occurred_at<=? ORDER BY sequence",
                (point_str,)).fetchall()
            import json as _json
            for row in candidate_rows:
                detail = _json.loads(row["detail_json"])
                belongs = (row["resource_type"] == "tour_plan" and row["resource_id"] == plan_id) \
                    or (row["resource_type"] == "tour_segment"
                        and row["resource_id"].startswith(f"{plan_id}:")) \
                    or (row["resource_type"] == "tour_incident" and detail.get("plan_id") == plan_id)
                if not belongs:
                    continue
                chain.append({"sequence": row["sequence"], "occurred_at": row["occurred_at"],
                              "actor_id": row["actor_id"], "action": row["action"],
                              "resource_type": row["resource_type"],
                              "resource_id": row["resource_id"], "detail": detail})
                if row["action"] == "tour.published":
                    effective_at = detail.get("version")
            # 时点时各版本状态
            version_states: dict[int, str] = {}
            for event in chain:
                detail = event["detail"]
                if "version" not in detail:
                    continue
                mapping = {"tour.draft_created": DRAFT, "tour.draft_blocked": BLOCKED,
                           "tour.rejected": REJECTED, "tour.published": PUBLISHED,
                           "tour.reroute_drafted": DRAFT, "tour.reroute_blocked": BLOCKED,
                           "tour.lease_expired": BLOCKED}
                if event["action"] in mapping:
                    version_states[detail["version"]] = mapping[event["action"]]
                if event["action"] == "tour.published" and detail.get("superseded_version"):
                    version_states[detail["superseded_version"]] = SUPERSEDED
            # 时点时的确认链
            confirmations = []
            if effective_at is None:
                draft_versions = sorted(v for v, s in version_states.items() if s == DRAFT)
                target_version = draft_versions[-1] if draft_versions else max(r["version"] for r in versions)
            else:
                target_version = effective_at
            for row in conn.execute(
                    "SELECT * FROM plan_confirmations WHERE plan_id=? AND version=? "
                    "ORDER BY party_kind,party_ref",
                    (plan_id, target_version)).fetchall():
                if row["decided_at"] and row["decided_at"] <= point_str:
                    confirmations.append({"party_kind": row["party_kind"], "party_ref": row["party_ref"],
                                          "step": row["step"], "status": row["status"],
                                          "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                                          "comment": row["comment"]})
            handovers = []
            for row in conn.execute(
                    "SELECT * FROM segment_handovers WHERE plan_id=? AND completed_at<=? ORDER BY completed_at",
                    (plan_id, point_str)).fetchall():
                handovers.append(dict(row))
            return {
                "plan_id": plan_id,
                "at": point_str,
                "effective_version": effective_at,
                "version_states": version_states,
                "responsibility_chain": chain,
                "confirmations": confirmations,
                "handovers": handovers,
            }

