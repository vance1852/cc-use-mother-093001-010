# 调度获奖作品巡展渠道协作服务

本项目在文化创意赛事共享基础能力（主体、操作者、幂等、SQLite 事务、哈希审计链）之上，
建设获奖作品商场 / 博物馆 / 茶文化空间巡展的**巡展与渠道调度服务**。

它把作品及组件、场馆开放窗口、展柜条件、布撤展工时、运输区段、承运能力、保险额度、
接收资质与宣传承诺纳入同一套**计划版本**，用限时租约先占用资源，按职责顺序确认后才发布；
任何拒签、窗口缩短、运输延误、作品损伤、临时闭馆都只重排受影响区段，已完成交接、
已发生费用与公开排期不会被静默覆盖，备用场馆与运输方案按 options 中的稳定优先级启用。

## 核心规则

- **限时草案占用**：草案以租约（held，默认 5 分钟）占用窗口、展柜、工时、承运区段、保险额度；
  到期未完成确认自动失效并释放，重启后通过 `reconcile` 协调。
- **按职责顺序确认**：作品代表 → 保险经办 → 场馆 → 承运方，固定步骤顺序；
  未轮到的一方不能确认，重复确认返回 `duplicate`，不续租、不多占资源。
- **发布互斥**：同一计划并发发布最多一个版本生效（`plan_effective` + `BEGIN IMMEDIATE`）。
- **只重排受影响区段**：事故触发下一草案版本，只切换受影响停点 / 区段；
  已 `delivered` 的区段、历史费用、公开排期不可静默覆盖；未变动的一方不参与重确认。
- **稳定优先级备用**：每个停点 / 区段的 `options` 数组即优先级，系统按序尝试并记录每次尝试的阻塞原因。
- **观众排期保护**：改期会计算每个受影响观众场次；已公告场次必须显式 `notice_confirmed=true` 才能重排。
- **最小必要资料**：各参与方 HTTP 待办只返回其确认所需的资料（承运方只看自己的区段等）。
- **可追责可还原**：全部状态变更写入哈希串联审计链；审计员可按历史时点还原生效版本、确认链与交接记录。

## 目录

- src/creative_program_foundation/
  - `service.py` / `storage.py` / `audit.py`：基础主体、事务与审计；
  - `tour.py`：巡展与渠道调度领域服务（租约、确认、发布、重排、查询、时点还原）；
  - `tour_acceptance.py`：巡展服务离线验收；
  - `api.py`：HTTP/JSON 路由（基础接口 + `/tour/*`）。
- tests/：基础规则与巡展规则（含双重预订、租约到期、闭馆备用切换、运输延误、
  费用保留、并发发布、重启一致性、时点还原）。

## 环境

- Linux，Python 3.11 或更高版本，仅使用标准库和 SQLite。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.tour_acceptance

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

所有写接口通过 `X-Actor-Id` 标识操作者，并要求幂等 `request_id`。健康检查 `GET /health`。

### 渠道资源登记（运营）

- `POST /tour/artworks` 作品及组件（重量/体积/件数/工时/恒温安保要求、作品代表、声明价值）
- `POST /tour/venues`、`/tour/venue-windows`、`/tour/display-cases`、`/tour/labor`、`/tour/receivers`
- `POST /tour/carriers`、`/tour/carrier-slots`（承运能力：重量/体积/件数）
- `POST /tour/insurance-policies`（额度与有效期）

### 计划生命周期

- `POST /tour/plans/drafts`：提交 manifest（含 stops/segments 的 options 优先级、保险、观众场次、宣传承诺）
- `POST /tour/plans/confirmations`：按顺序确认/拒签
- `POST /tour/plans/publish`：全部确认后发布
- `POST /tour/plans/replan`：手动按备用选项重排
- `POST /tour/incidents`：上报 `transport_delay` / `artwork_damaged`
- `POST /tour/venue-windows/shorten`、`POST /tour/venues/close`：场馆窗口变动，自动触发重排
- `POST /tour/segments/dispatch`、`POST /tour/segments/handover`：发运与合规接收人交接

### 查询

- `GET /tour/todos`：当前操作者的待办（确认 / 在途交接 / 阻塞草案 / 开放事故）及最小资料
- `GET /tour/plans/{id}`：版本、生效版本、停点与区段运行状态
- `GET /tour/conflicts?plan_id=`：阻塞原因、阻塞方租约、恢复路径与每次备用尝试
- `GET /tour/session-changes?plan_id=`：每次改期影响的观众场次
- `GET /tour/costs?plan_id=`：只增不改的已发生费用
- `GET /tour/restore?plan_id=&at=`：按历史时点还原责任链（审计/运营）

服务重启后 SQLite 中的生效版本、confirmed 租约、在途 / 已交接状态和审计历史继续保留。
