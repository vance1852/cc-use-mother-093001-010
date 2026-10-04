"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database
from .tour import TourService


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          tour: TourService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    def call(method_name: str, *names: str, optional: tuple[str, ...] = ()):
        kwargs = {"actor_id": actor_id}
        for name in names:
            if name not in body:
                raise ValidationError(f"缺少必填字段 {name}")
            kwargs[name] = body[name]
        for name in optional:
            if name in body:
                kwargs[name] = body[name]
        return getattr(tour, method_name)(**kwargs)

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = q("category")
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0") or "0")
            return 200, {"items": service.audit_events(after)}

        # ------------------------------------------------ 巡展调度接口
        if tour is not None:
            payload, status = _tour_route(tour, method, parsed.path, body, actor_id, query, call, q)
            if payload is not None:
                return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _tour_route(tour: TourService, method: str, path: str, body: dict[str, Any], actor_id: str,
                query, call, q):
    """巡展子路由。返回 (payload, status)；未命中返回 (None, None)。"""

    if method == "POST":
        table = {
            "/tour/artworks": ("register_artwork",
                               ("request_id", "artwork_id", "title", "representative_id",
                                "declared_value", "spec")),
            "/tour/venues": ("register_venue",
                             ("request_id", "venue_id", "site_id", "name",
                              "contact_actor_id", "required_qualification")),
            "/tour/venue-windows": ("register_venue_window",
                                    ("request_id", "window_id", "venue_id", "starts_at", "ends_at")),
            "/tour/display-cases": ("register_display_case",
                                    ("request_id", "case_id", "venue_id", "label", "conditions")),
            "/tour/labor": ("register_labor",
                            ("request_id", "labor_id", "venue_id", "starts_at", "ends_at",
                             "available_hours")),
            "/tour/receivers": ("register_receiver",
                                ("request_id", "receiver_id", "venue_id", "receiver_actor_id",
                                 "qualification")),
            "/tour/carriers": ("register_carrier",
                               ("request_id", "carrier_id", "name", "contact_actor_id")),
            "/tour/carrier-slots": ("register_carrier_slot",
                                    ("request_id", "slot_id", "carrier_id", "starts_at", "ends_at",
                                     "capacity")),
            "/tour/insurance-policies": ("register_insurance_policy",
                                         ("request_id", "policy_id", "name", "handler_actor_id",
                                          "limit_amount", "starts_at", "ends_at")),
            "/tour/plans/drafts": ("create_draft", ("request_id", "manifest")),
            "/tour/plans/confirmations": ("confirm",
                                          ("request_id", "plan_id", "version"),
                                          ("approved", "comment")),
            "/tour/plans/publish": ("publish", ("request_id", "plan_id", "version")),
            "/tour/plans/replan": ("replan", ("request_id", "plan_id"),
                                   ("stop_overrides", "segment_overrides", "notice_confirmed")),
            "/tour/incidents": ("report_incident", ("request_id", "plan_id", "kind"),
                                ("stop_keys", "segment_keys", "detail", "notice_confirmed")),
            "/tour/venue-windows/shorten": ("shorten_venue_window",
                                            ("request_id", "window_id", "new_starts_at",
                                             "new_ends_at", "reason"),
                                            ("notice_confirmed",)),
            "/tour/venues/close": ("close_venue", ("request_id", "venue_id", "reason"),
                                   ("notice_confirmed",)),
            "/tour/segments/dispatch": ("dispatch_segment",
                                        ("request_id", "plan_id", "segment_key")),
            "/tour/segments/handover": ("complete_handover",
                                        ("request_id", "plan_id", "segment_key", "condition_note")),
        }
        if path in table:
            entry = table[path]
            method_name, names = entry[0], entry[1]
            optional = entry[2] if len(entry) == 3 else ()
            result = call(method_name, *names, optional=optional)
            return result, 200 if result.get("replayed") else 201
    if method == "GET":
        if path == "/tour/todos":
            actor = q("actor_id") or actor_id
            if not actor:
                raise ValidationError("X-Actor-Id 或 actor_id 不能为空")
            return {"items": tour.list_todos(actor)}, 200
        if path.startswith("/tour/plans/"):
            plan_id = path.rsplit("/", 1)[-1]
            return tour.get_plan(plan_id, actor_id=actor_id or None), 200
        if path == "/tour/conflicts":
            plan_id = q("plan_id", "")
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return tour.get_conflicts(plan_id, actor_id=actor_id or None), 200
        if path == "/tour/session-changes":
            plan_id = q("plan_id", "")
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return tour.get_session_changes(plan_id, actor_id=actor_id or None), 200
        if path == "/tour/costs":
            plan_id = q("plan_id", "")
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return tour.get_costs(plan_id, actor_id=actor_id or None), 200
        if path == "/tour/restore":
            plan_id = q("plan_id", "")
            at = q("at", "")
            if not plan_id or not at:
                raise ValidationError("plan_id 与 at 不能为空")
            return tour.restore_at(plan_id, at, actor_id=actor_id or None), 200
    return None, None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    tour: TourService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                tour=self.tour)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.tour = TourService(database)
    # 重启后清理到期租约，使资源占用、在途状态与待确认顺序保持一致
    Handler.tour.reconcile()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
