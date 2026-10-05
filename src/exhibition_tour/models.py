"""定义巡展调度服务在模块边界使用的数据对象与常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 参与角色，对应 X-Actor-Role 声明的职责。
ROLE_OPERATOR = "operator"        # 运营人员
ROLE_VENUE = "venue"              # 场馆窗口负责方
ROLE_CARRIER = "carrier"          # 承运方
ROLE_INSURER = "insurer"          # 保险经办
ROLE_REPRESENTATIVE = "representative"  # 作品代表
ROLE_AUDITOR = "auditor"          # 审计员

CONFIRM_ROLES = (ROLE_VENUE, ROLE_CARRIER, ROLE_INSURER, ROLE_REPRESENTATIVE)
ALL_ROLES = frozenset((ROLE_OPERATOR, *CONFIRM_ROLES, ROLE_AUDITOR))

# 参与方角色 -> 计划中需要其确认的区段类型。
ROLE_SEGMENT_TYPES: dict[str, frozenset[str]] = {
    ROLE_VENUE: frozenset({"venue"}),
    ROLE_CARRIER: frozenset({"transport"}),
    ROLE_INSURER: frozenset({"insurance"}),
    ROLE_REPRESENTATIVE: frozenset({"venue", "transport", "insurance"}),
}

# 区段种类。
SEG_VENUE = "venue"
SEG_TRANSPORT = "transport"
SEG_INSURANCE = "insurance"
SEGMENT_KINDS = frozenset({SEG_VENUE, SEG_TRANSPORT, SEG_INSURANCE})

# 计划版本状态。
PLAN_DRAFT = "draft"            # 限时草案，持有资源租约
PLAN_CONFIRMED = "confirmed"    # 各方确认完成，尚未发布
PLAN_PUBLISHED = "published"    # 已发布，最多一个版本生效
PLAN_REJECTED = "rejected"      # 被某方拒绝，等待重排
PLAN_SUPERSEDED = "superseded"  # 被后继版本取代
PLAN_EXPIRED = "expired"        # 草案租约超时

# 区段/交接状态。
SEG_PENDING = "pending"
SEG_RESCHEDULED = "rescheduled"  # 已被后继版本重排，仅历史保留
SEG_HANDED_OVER = "handed_over"  # 已完成实物交接，不可静默覆盖

# 待办动作。
TODO_CONFIRM = "confirm"
TODO_REJECT = "reject"

# 变更来源（重排触发方）。
REWORK_REJECTION = "rejection"
REWORK_WINDOW_SHORTENED = "window_shortened"
REWORK_TRANSPORT_DELAY = "transport_delay"
REWORK_DAMAGE = "damage"
REWORK_VENUE_CLOSED = "venue_closed"

REWORK_REASONS = frozenset({
    REWORK_REJECTION,
    REWORK_WINDOW_SHORTENED,
    REWORK_TRANSPORT_DELAY,
    REWORK_DAMAGE,
    REWORK_VENUE_CLOSED,
})


@dataclass(frozen=True)
class Participant:
    """参与方账号：绑定角色与可选的组织（场馆/承运/保险机构）。"""

    participant_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Artwork:
    """作品及其可运输组件。"""

    artwork_id: str
    title: str
    representative_id: str
    components: tuple[str, ...]
    condition_note: str


@dataclass(frozen=True)
class Resource:
    """可被占用的渠道资源：场馆窗口、展柜、车辆、保险额度。"""

    resource_id: str
    kind: str
    owner_id: str
    label: str
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReceiverQualification:
    """场馆对某类作品的接收资质（无有效资质的场馆不能合规接收）。"""

    venue_organization_id: str
    artwork_id: str
    qualified: bool
    certificate: str


@dataclass(frozen=True)
class Segment:
    """计划中的一个可调度区段。"""

    segment_id: str
    kind: str
    start_at: str
    end_at: str
    resource_id: str
    requirement: dict[str, Any] = field(default_factory=dict)
    status: str = SEG_PENDING
    handover_at: str | None = None
    handover_by: str | None = None
    handover_ref: str | None = None
    alternatives: tuple[str, ...] = ()
    # 发布后固化的快照字段：
    confirmed_at: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class AudienceSession:
    """公开排期的观众场次。"""

    session_id: str
    segment_id: str
    starts_at: str
    label: str
    capacity: int


@dataclass(frozen=True)
class PlanVersion:
    """一个巡展计划版本（草案/确认链/发布的载体）。"""

    plan_id: str
    version: int
    status: str
    artwork_id: str
    components: tuple[str, ...]
    created_by: str
    created_at: str
    lease_expires_at: str | None
    confirmed_at: str | None = None
    published_at: str | None = None
    superseded_at: str | None = None
    rejected_by: str | None = None
    reject_reason: str | None = None
    rework_of_version: int | None = None
    rework_reason: str | None = None
    change_summary: str | None = None
    parent_segment_id: str | None = None
    segments: tuple[Segment, ...] = ()
    sessions: tuple[AudienceSession, ...] = ()
    # 责任链快照：版本上每一次确认/拒绝/交接/费用的留痕序号
    timeline: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Todo:
    """参与方待办及其最小必要资料。"""

    todo_id: str
    plan_id: str
    version: int
    role: str
    segment_kind: str
    action: str
    resource_id: str
    segment_start: str
    segment_end: str
    minimal: dict[str, Any]
    expires_at: str | None


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool
