# 巡展与渠道调度服务

在 `creative_program_foundation` 基础能力之上，为获奖作品商场、博物馆、茶文化空间巡展提供
渠道调度领域服务：把作品及组件、场馆窗口、展柜条件、布撤展工时、运输区段、承运能力、
保险额度、接收资质与宣传承诺纳入同一计划版本。

## 核心规则

- **限时草案占用**：计划以草案创建，按租约（默认 30 分钟，最长 1440 分钟）预占场馆窗口、
  展柜、车辆与保险额度；到期未发布自动释放（`sweep-expired`，写请求也会即时清理）。
- **按职责确认**：场馆方、承运方、保险经办、作品代表对各自区段确认后才可发布；
  重复确认按请求幂等，不会多占资源；跨机构不能确认他方待办。
- **并发唯一生效**：全部写操作在 `BEGIN IMMEDIATE` 事务内完成；并发发布最多一个版本生效，
  其余返回 409。
- **只重排受影响区段**：拒绝、窗口缩短、运输延误、作品损伤、临时闭馆只能生成新版本替换
  受影响区段；未受影响区段携带复用：
  - 已发布底座：全部携带（含已完成交接事实）；
  - 被拒底座：仅已全部确认的区段携带，未确认区段重建；
  - 过期底座：租约已释放，全部重建。
- **不可静默覆盖**：已完成交接禁止重排（须改后继区段）；已发生费用只能前向关联到新版本，
  金额不改写；公开场次只能标注 `shifted/cancelled` 并给出新旧时间，不能删除。
- **稳定优先级备用**：显式指定 → `prefer_resources` → 旧备选 → 同类型按创建时间/编号，
  自动匹配合规展柜（同城、同机构），并校验接收资质、承运能力、保险币种与额度。
- **最小必要资料**：各参与方通过 HTTP 只拿到自己待办与履职所需字段（如承运方看不到
  保险额度明细）。
- **可追溯**：版本时间线记录确认/拒绝/发布/交接/费用/改期影响；审计员可按历史时点
  还原责任链；全局哈希审计链可离线校验。
- **重启一致**：租约、在途交接、待确认顺序全部落盘，重启后保持。

## 目录

- `src/exhibition_tour/`：模型常量、SQLite 存储、调度领域服务、哈希审计、HTTP 路由、离线验收。
- `tests/test_tour_*.py`：领域规则、HTTP 边界与存储层测试。

## 测试 / 验收 / 服务

    PYTHONPATH=src python3 -m unittest discover -s tests -v
    python3 -m compileall -q src tests
    PYTHONPATH=src python3 -m exhibition_tour.acceptance
    PYTHONPATH=src python3 -m exhibition_tour.api --database tour.sqlite3 --host 127.0.0.1 --port 8090

## 主要 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/participants` `/artworks` `/resources` `/qualifications` | 建档登记 |
| POST | `/plans` | 创建限时草案版本 |
| GET | `/todos` | 当前参与方待办与最小必要资料 |
| POST | `/plans/{id}/respond` | 确认或拒绝（`confirm`/`reject`） |
| POST | `/plans/{id}/publish` | 全部确认后发布 |
| POST | `/plans/{id}/rework` | 按原因重排受影响区段 |
| POST | `/plans/{id}/abort` | 中止草案/被拒版本并释放全部租约 |
| GET | `/plans/{id}/recovery` | 冲突原因、恢复候选与下一步 |
| GET | `/plans/{id}/sessions-impact` | 每次改期影响的观众场次 |
| GET | `/plans/{id}/snapshot?at=...` | 历史时点责任链还原 |
| GET | `/availability?kind=&start_at=&end_at=` | 资源可用与占用来源 |
| POST | `/handovers` `/costs` | 交接凭证与已发生费用 |
| POST | `/publicity/{id}/resolve` | 宣传承诺履约/违约 |
| GET | `/audit-events` `/health` | 审计事件与健康检查 |

写入接口通过 `X-Actor-Id` 标识参与方，并要求幂等的 `request_id`。
