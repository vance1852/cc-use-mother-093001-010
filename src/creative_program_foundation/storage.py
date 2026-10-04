"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 巡展调度：作品、场馆、承运、保险等渠道资源
CREATE TABLE IF NOT EXISTS artworks (
    artwork_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    representative_id TEXT NOT NULL REFERENCES actors(actor_id),
    declared_value REAL NOT NULL CHECK(declared_value >= 0),
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venues (
    venue_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    contact_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    required_qualification TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venue_windows (
    window_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(venue_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','shortened','closed')),
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS display_cases (
    case_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(venue_id),
    label TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venue_labor_slots (
    labor_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(venue_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    available_hours REAL NOT NULL CHECK(available_hours > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receivers (
    receiver_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(venue_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    qualification TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(venue_id, actor_id)
);
CREATE TABLE IF NOT EXISTS carriers (
    carrier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS carrier_slots (
    slot_id TEXT PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','suspended')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insurance_policies (
    policy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    handler_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    limit_amount REAL NOT NULL CHECK(limit_amount >= 0),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','suspended')),
    created_at TEXT NOT NULL
);
-- 巡展计划及其版本快照
CREATE TABLE IF NOT EXISTS tour_plans (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','blocked','rejected','published','superseded','cancelled')),
    rep_actor_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    parent_version INTEGER,
    content_hash TEXT NOT NULL,
    lease_expires_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY(plan_id, version)
);
CREATE TABLE IF NOT EXISTS plan_effective (
    plan_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_stops_snapshot (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stop_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, version, stop_key)
);
CREATE TABLE IF NOT EXISTS plan_segments_snapshot (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, version, segment_key)
);
-- 跨版本稳定的资源租约与运行状态
CREATE TABLE IF NOT EXISTS resource_allocations (
    allocation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    amount REAL,
    status TEXT NOT NULL CHECK(status IN ('held','confirmed','released','expired','blocked')),
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocations_resource
    ON resource_allocations(resource_type, resource_id, status);
CREATE TABLE IF NOT EXISTS plan_confirmations (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    party_kind TEXT NOT NULL,
    party_ref TEXT NOT NULL,
    step INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
    instruction TEXT NOT NULL DEFAULT '',
    decided_by TEXT,
    decided_at TEXT,
    comment TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, version, party_kind, party_ref)
);
CREATE TABLE IF NOT EXISTS stop_states (
    plan_id TEXT NOT NULL,
    stop_key TEXT NOT NULL,
    state TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    handover_completed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, stop_key)
);
CREATE TABLE IF NOT EXISTS segment_states (
    plan_id TEXT NOT NULL,
    segment_key TEXT NOT NULL,
    state TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, segment_key)
);
CREATE TABLE IF NOT EXISTS segment_handovers (
    handover_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    segment_key TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    condition_note TEXT NOT NULL,
    completed_by TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_costs (
    cost_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    ref_key TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audience_sessions (
    session_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    stop_key TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    announced INTEGER NOT NULL CHECK(announced IN (0,1)),
    announced_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('scheduled','rescheduled','cancelled'))
);
CREATE TABLE IF NOT EXISTS session_changes (
    change_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK(change_kind IN ('reschedule','cancel')),
    old_starts_at TEXT,
    old_ends_at TEXT,
    new_starts_at TEXT,
    new_ends_at TEXT,
    reason TEXT NOT NULL,
    notice_state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_incidents (
    incident_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    resolution_json TEXT
);
CREATE TABLE IF NOT EXISTS plan_conflicts (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, version)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # 单连接服务多线程：进程内串行化写事务，BEGIN IMMEDIATE 继续负责跨进程互斥
        self._tx_lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        with self._tx_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
