"""封装 SQLite 连接、建表和事务边界。

所有业务状态都落盘，服务重启后资源租约、在途交接状态与待确认顺序保持一致。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

-- 参与方账号 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 作品与组件 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artworks (
    artwork_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    representative_id TEXT NOT NULL REFERENCES participants(participant_id),
    components_json TEXT NOT NULL,
    condition_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 渠道资源（场馆窗口/展柜、运输车辆、保险额度） -----------------------------
-- kind: venue_window | display_case | vehicle | insurance_quota
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES participants(participant_id),
    label TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 场馆接收资质（场馆组织 + 作品） ------------------------------------------
CREATE TABLE IF NOT EXISTS receiver_qualifications (
    venue_organization_id TEXT NOT NULL,
    artwork_id TEXT NOT NULL REFERENCES artworks(artwork_id),
    qualified INTEGER NOT NULL CHECK(qualified IN (0,1)),
    certificate TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue_organization_id, artwork_id)
);

-- 巡展计划与版本 -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    artwork_id TEXT NOT NULL REFERENCES artworks(artwork_id),
    current_version INTEGER NOT NULL DEFAULT 0,
    published_version INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    components_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lease_expires_at TEXT,
    confirmed_at TEXT,
    published_at TEXT,
    superseded_at TEXT,
    rejected_by TEXT,
    reject_reason TEXT,
    rework_of_version INTEGER,
    rework_reason TEXT,
    change_summary TEXT,
    parent_segment_id TEXT,
    PRIMARY KEY (plan_id, version)
);

-- 区段 ---------------------------------------------------------------------
-- kind: venue | transport | insurance
-- 携带到后继版本的区段复用同一 segment_id，故主键含版本。
CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    requirement_json TEXT NOT NULL,
    status TEXT NOT NULL,
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    confirmed_at TEXT,
    published_at TEXT,
    handover_at TEXT,
    handover_by TEXT,
    handover_ref TEXT,
    PRIMARY KEY (plan_id, version, segment_id),
    FOREIGN KEY (plan_id, version) REFERENCES plan_versions(plan_id, version)
);
CREATE INDEX IF NOT EXISTS idx_segments_segment ON segments(segment_id);

-- 资源占用（租约/确认/发布） -----------------------------------------------
-- status: held | released | committed
CREATE TABLE IF NOT EXISTS resource_holds (
    hold_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(segment_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_holds_resource ON resource_holds(resource_id);
CREATE INDEX IF NOT EXISTS idx_holds_lookup
    ON resource_holds(resource_id, status, start_at, end_at);

-- 待办（每角色每区段一条，重复确认按该表幂等，不会多占资源） ---------------
CREATE TABLE IF NOT EXISTS todos (
    todo_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    role TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    segment_kind TEXT NOT NULL,
    action_state TEXT NOT NULL,           -- pending | confirmed | rejected
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    sequence INTEGER NOT NULL,           -- 待确认顺序（同版本内稳定排序）
    UNIQUE(plan_id, version, role, segment_id)
);

-- 观众场次（公开排期） -----------------------------------------------------
CREATE TABLE IF NOT EXISTS audience_sessions (
    session_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    label TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    publicity_state TEXT NOT NULL,       -- announced | carried | shifted | cancelled
    announced_at TEXT,
    affected_by_event_id TEXT,
    FOREIGN KEY (plan_id, version) REFERENCES plan_versions(plan_id, version)
);
CREATE INDEX IF NOT EXISTS idx_sessions_segment ON audience_sessions(segment_id);

-- 已发生费用（不可静默覆盖；重排只能追加关联） -----------------------------
CREATE TABLE IF NOT EXISTS incurred_costs (
    cost_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    category TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    carried_into_version INTEGER,
    note TEXT NOT NULL DEFAULT ''
);

-- 宣传承诺（场馆方对运营方的公开宣传义务） ---------------------------------
CREATE TABLE IF NOT EXISTS publicity_commitments (
    commitment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    venue_segment_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    promised_at TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL                  -- promised | fulfilled | broken | carried
);

-- 版本责任链时间线（确认/拒绝/发布/交接/费用/改期影响的留痕） --------------
CREATE TABLE IF NOT EXISTS version_timeline (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, version, sequence)
);

-- 幂等回执 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 哈希串联审计 -------------------------------------------------------------
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
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()
