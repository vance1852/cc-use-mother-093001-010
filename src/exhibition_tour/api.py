"""巡展调度服务的 HTTP/JSON 边界（仅使用 Python 标准库）。

参与方通过 X-Actor-Id 标识；GET 接口返回自己的待办与最小必要资料。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import TourError
from .service import TourScheduler
from .storage import Database


def route(service: TourScheduler, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到调度服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        if method == "POST" and parsed.path == "/participants":
            receipt = service.register_participant(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/artworks":
            receipt = service.register_artwork(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/resources":
            receipt = service.register_resource(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/qualifications":
            receipt = service.upsert_qualification(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/plans":
            receipt = service.create_plan_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # /plans/{plan_id}/... 子资源
        if len(segments) == 3 and segments[0] == "plans":
            plan_id = segments[1]
            action = segments[2]
            if method == "POST" and action == "respond":
                receipt = service.respond(actor_id=actor_id, plan_id=plan_id, **body)
                return 200 if receipt.replayed else 200, receipt.__dict__
            if method == "POST" and action == "publish":
                receipt = service.publish(actor_id=actor_id, plan_id=plan_id, **body)
                return 200 if receipt.replayed else 200, receipt.__dict__
            if method == "POST" and action == "rework":
                receipt = service.rework_plan(actor_id=actor_id, plan_id=plan_id, **body)
                return 200 if receipt.replayed else 201, receipt.__dict__
            if method == "POST" and action == "abort":
                receipt = service.abort_version(actor_id=actor_id, plan_id=plan_id, **body)
                return 200, receipt.__dict__
            if method == "GET" and action == "recovery":
                return 200, service.recovery_options(actor_id=actor_id, plan_id=plan_id)
            if method == "GET" and action == "sessions-impact":
                return 200, service.sessions_impact(actor_id=actor_id, plan_id=plan_id)
            if method == "GET" and action == "snapshot":
                at = query.get("at", [""])[0]
                if not at:
                    raise ValueError("at 不能为空")
                return 200, service.snapshot_at(actor_id=actor_id, plan_id=plan_id, at=at)

        if method == "GET" and len(segments) == 2 and segments[0] == "plans":
            return 200, service.get_plan(actor_id=actor_id, plan_id=segments[1])

        if method == "POST" and parsed.path == "/handovers":
            receipt = service.record_handover(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/costs":
            receipt = service.record_cost(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "publicity" \
                and segments[2] == "resolve":
            receipt = service.resolve_publicity(actor_id=actor_id, commitment_id=segments[1], **body)
            return 200 if receipt.replayed else 200, receipt.__dict__
        if method == "POST" and parsed.path == "/sweep-expired":
            return 200, service.sweep_expired(actor_id=actor_id)

        if method == "GET" and parsed.path == "/todos":
            return 200, {"items": service.list_todos(actor_id=actor_id)}
        if method == "GET" and parsed.path == "/availability":
            def one(name: str, required: bool = True) -> str | None:
                value = query.get(name, [None])[0]
                if required and not value:
                    raise ValueError(f"{name} 不能为空")
                return value
            coverage = query.get("coverage", [None])[0]
            return 200, service.availability(
                kind=one("kind"), start_at=one("start_at"), end_at=one("end_at"),
                city=query.get("city", [None])[0],
                coverage=float(coverage) if coverage is not None else None)
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except TourError as exc:
        payload = {"error": exc.code, "message": str(exc)}
        blockers = getattr(exc, "blockers", None)
        if blockers is not None:
            payload["blockers"] = blockers
        return exc.status, payload
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: TourScheduler

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
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

    parser = argparse.ArgumentParser(description="启动巡展与渠道调度服务")
    parser.add_argument("--database", default="tour.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = TourScheduler(database)
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
