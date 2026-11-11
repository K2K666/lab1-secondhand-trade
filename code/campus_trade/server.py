"""HTTP adapter for the loopback campus application."""

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import accounts, operations, products, reservations
from .core import (
    ApiError,
    Context,
    WriteQueue,
    audit,
    database,
    integer,
    write_transaction,
)

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
MAX_REQUEST_BYTES = 64 * 1024 * 1024
COOKIE_NAME = "campus_session"
ASSET_NAME = re.compile(r"[a-zA-Z0-9_-]+\.(?:js|css|svg|png|ico)")
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
KEEP_COOKIE = object()


@dataclass
class Reply:
    body: object
    mime: str = "application/json; charset=utf-8"
    token: object = KEEP_COOKIE
    sequence: int | None = None


@dataclass(frozen=True)
class Route:
    pattern: re.Pattern
    action: str
    run: object
    object_type: str | None


def _read_routes():
    # Simple wrappers keep service signatures independent of HTTP.
    return (
        (r"/api/me", lambda db, ctx, q, m: accounts.whoami(db, ctx)),
        (r"/api/products", lambda db, ctx, q, m: products.list_products(db, ctx, q)),
        (
            r"/api/products/([0-9]+)",
            lambda db, ctx, q, m: products.product_detail(db, ctx, _id(m)),
        ),
        (r"/api/photos/([0-9]+)", _photo),
        (
            r"/api/reservations",
            lambda db, ctx, q, m: reservations.list_reservations(db, ctx, q),
        ),
        (r"/api/admin/reviews", lambda db, ctx, q, m: products.review_queue(db, ctx, q)),
        (r"/api/admin/reports", lambda db, ctx, q, m: products.report_queue(db, ctx, q)),
        (r"/api/admin/accounts", lambda db, ctx, q, m: accounts.list_admins(db, ctx)),
        (r"/api/stats", lambda db, ctx, q, m: operations.stats(db, ctx, q)),
        (r"/api/logs", lambda db, ctx, q, m: operations.list_logs(db, ctx, q)),
        (r"/api/notifications", lambda db, ctx, q, m: operations.list_notifications(db, ctx)),
    )


def _id(match):
    return integer(match.group(1), "编号")


def _photo(db, ctx, _query, match):
    mime, data = products.get_photo(db, ctx, _id(match))
    return Reply(data, mime)


def _login(db, ctx, data, token, _match):
    body, token = accounts.login(db, ctx, data, token)
    return Reply(body, token=token)


def _logout(db, _ctx, _data, token, _match):
    return Reply(accounts.logout(db, token), token=None)


def _password(db, ctx, data, _token, _match):
    body, token = accounts.change_password(db, ctx, data)
    return Reply(body, token=token)


def _reservation_action(db, ctx, data, _token, match):
    return reservations.reservation_action(db, ctx, _id(match), match.group(2), data)


def _mark_notification(db, ctx, _data, _token, match):
    return operations.mark_read(db, ctx, _id(match))


def _write_routes():
    records = [
        (r"/api/auth/code", "issue_code", _plain(accounts.issue_code), "account"),
        (r"/api/auth/register", "register", _plain(accounts.register), "account"),
        (r"/api/auth/login", "login", _login, "account"),
        (r"/api/auth/logout", "logout", _logout, "account"),
        (r"/api/auth/password", "change_password", _password, "account"),
        (r"/api/auth/profile", "update_profile", _plain(accounts.update_profile), "account"),
        (r"/api/admin/accounts", "create_admin", _plain(accounts.create_admin), "account"),
        (r"/api/products", "product.create", _plain(products.create_draft), "product"),
        (
            r"/api/products/([0-9]+)/edit",
            "product.edit",
            _target(products.edit_product),
            "product",
        ),
        (
            r"/api/products/([0-9]+)/submit",
            "product.submit",
            _target(products.submit_product),
            "product",
        ),
        (
            r"/api/products/([0-9]+)/downlist",
            "product.downlist",
            _target(products.downlist_product),
            "product",
        ),
        (
            r"/api/products/([0-9]+)/appeal",
            "appeal.submit",
            _target(products.submit_appeal),
            "product",
        ),
        (
            r"/api/products/([0-9]+)/report",
            "report.create",
            _target(products.report_product),
            "product",
        ),
        (
            r"/api/products/([0-9]+)/reserve",
            "reservation.create",
            _target(reservations.create_reservation),
            "product",
        ),
        (
            r"/api/reservations/([0-9]+)/(confirm|reject|cancel|complete)",
            "reservation",
            _reservation_action,
            "reservation",
        ),
        (
            r"/api/admin/reviews/([0-9]+)",
            "review_product",
            _target(products.review_product),
            "review",
        ),
        (
            r"/api/admin/reports/([0-9]+)",
            "handle_report",
            _target(products.handle_report),
            "report",
        ),
        (
            r"/api/admin/products/([0-9]+)/violate",
            "product.violate",
            _target(products.violate_product),
            "product",
        ),
        (
            r"/api/notifications/([0-9]+)/read",
            "mark_notification_read",
            _mark_notification,
            "notification",
        ),
    ]
    return tuple(Route(re.compile(path), action, run, kind) for path, action, run, kind in records)


def _plain(function):
    return lambda db, ctx, data, token, match: function(db, ctx, data)


def _target(function):
    return lambda db, ctx, data, token, match: function(db, ctx, _id(match), data)


class CampusServer(ThreadingHTTPServer):
    """Single-process writes and concurrent read-only requests."""

    daemon_threads = True

    def __init__(self, path, port, *, now=None, scheduler_interval=30):
        self.path = path
        self.now = now or (lambda: int(time.time()))
        self.writes = WriteQueue()
        self.stop_worker = threading.Event()
        self.scheduler_interval = scheduler_interval
        super().__init__(("127.0.0.1", port), CampusHandler)

    def run_due(self):
        with self.writes.turn(), database(self.path) as db, write_transaction(db):
            return reservations.expire_due(db, Context(timestamp=self.now()))

    def start_scheduler(self):
        def run():
            while not self.stop_worker.is_set():
                try:
                    self.run_due()
                except sqlite3.Error:
                    LOG.exception("预约超时检查失败，下次会重试")
                self.stop_worker.wait(self.scheduler_interval)

        thread = threading.Thread(target=run, name="reservation-expiry", daemon=True)
        thread.start()
        return thread

    def server_close(self):
        self.stop_worker.set()
        super().server_close()


class CampusHandler(BaseHTTPRequestHandler):
    server_version = "CampusTrade/1"
    sys_version = ""

    def setup(self):
        self.request.settimeout(12)
        super().setup()

    def do_GET(self):
        self._handle(write=False)

    def do_POST(self):
        self._handle(write=True)

    def log_message(self, _format, *_args):
        # Avoid recording query data in process logs. Business audit is structured.
        pass

    def _handle(self, *, write):
        try:
            self._validate_host(write)
            parts = urlsplit(self.path)
            if parts.scheme or parts.netloc or len(self.path) > 4096:
                raise ApiError(400, "请求地址无效")
            if not write and not parts.path.startswith("/api/"):
                reply = self._asset(parts.path)
            elif write:
                reply = self._write(parts.path, self._body())
            else:
                reply = self._read(parts.path, parts.query)
            self._send(HTTPStatus.OK, reply)
        except ApiError as error:
            self._send(error.status, Reply({"error": error.message, "code": error.code}))
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                self._send(503, Reply({"error": "数据库暂时繁忙，请稍后重试", "code": "busy"}))
            else:
                self._unexpected()
        except Exception:
            self._unexpected()

    def _unexpected(self):
        LOG.exception("处理本地请求时发生异常")
        self._send(500, Reply({"error": "系统暂时无法处理此请求", "code": "internal_error"}))

    def _validate_host(self, write):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        host = self.headers.get("Host")
        if host not in hosts:
            raise ApiError(403, "只接受本机访问", "invalid_host")
        if write:
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{host}":
                raise ApiError(403, "请求来源无效", "invalid_origin")

    def _asset(self, path):
        name = "index.html" if path == "/" else path.removeprefix("/static/")
        if name != "index.html" and not ASSET_NAME.fullmatch(name):
            raise ApiError(404, "页面不存在", "not_found")
        resource = STATIC / name
        if not resource.is_file():
            raise ApiError(404, "页面不存在", "not_found")
        mime = ASSET_TYPES[resource.suffix]
        return Reply(resource.read_bytes(), mime)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "请求传输格式无效")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "请求须使用 JSON")
        length = integer(
            self.headers.get("Content-Length"),
            "请求长度",
            minimum=1,
            maximum=MAX_REQUEST_BYTES,
        )
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeError, ValueError, RecursionError) as error:
            raise ApiError(400, "请求 JSON 无效") from error
        if not isinstance(data, dict):
            raise ApiError(400, "请求 JSON 须为对象")
        return data

    def _token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        session = cookies.get(COOKIE_NAME)
        return session.value if session else None

    def _identity(self, db, token):
        return accounts.context_for(db, token, self.client_address[0], self.server.now())

    def _read(self, path, raw_query):
        try:
            query = dict(parse_qsl(raw_query, keep_blank_values=True, max_num_fields=30))
        except ValueError as error:
            raise ApiError(400, "查询参数过多") from error
        for pattern, run in READ_ROUTES:
            match = pattern.fullmatch(path)
            if match:
                with database(self.server.path) as db:
                    # Each read keeps a single SQLite snapshot for related queries.
                    db.execute("BEGIN")
                    body = run(db, self._identity(db, self._token()), query, match)
                return body if isinstance(body, Reply) else Reply(body)
        raise ApiError(404, "接口不存在", "not_found")

    def _write(self, path, data):
        for route in WRITE_ROUTES:
            match = route.pattern.fullmatch(path)
            if match:
                return self._run_write(route, match, data)
        raise ApiError(404, "接口不存在", "not_found")

    def _run_write(self, route, match, data):
        token = self._token()
        with self.server.writes.turn() as sequence, database(self.server.path) as db:
            ctx = self._identity(db, token)
            try:
                with write_transaction(db):
                    body = route.run(db, ctx, data, token, match)
            except ApiError as error:
                self._failed_write(db, ctx, route, match, error.code)
                raise
            except sqlite3.Error:
                self._failed_write(db, ctx, route, match, "database_error")
                raise
        reply = body if isinstance(body, Reply) else Reply(body)
        reply.sequence = sequence
        return reply

    @staticmethod
    def _failed_write(db, ctx, route, match, code):
        groups = match.groups()
        ident = None
        if groups and len(groups[0]) < 11:
            ident = int(groups[0])
        product_id = ident if route.object_type == "product" else None
        if route.object_type == "reservation" and ident is not None:
            row = db.execute(
                "SELECT product_id FROM reservation WHERE id = ?",
                (ident,),
            ).fetchone()
            product_id = row["product_id"] if row else None
        action = f"reservation.{groups[1]}" if route.action == "reservation" else route.action
        try:
            with write_transaction(db):
                audit(
                    db,
                    ctx,
                    action,
                    object_type=route.object_type,
                    object_id=ident,
                    product_id=product_id,
                    success=False,
                    detail=code,
                )
        except sqlite3.Error:
            LOG.exception("失败操作未写入数据库审计")

    def _send(self, status, reply):
        payload = reply.body
        if reply.mime.startswith("application/json"):
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", reply.mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if reply.sequence is not None:
                self.send_header("X-Write-Sequence", str(reply.sequence))
            if reply.token is not KEEP_COOKIE:
                token = reply.token or ""
                age = accounts.SESSION_SECONDS if token else 0
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}",
                )
            self.end_headers()
            self.wfile.write(payload)
        except (TimeoutError, ConnectionError):
            self.close_connection = True


READ_ROUTES = tuple((re.compile(path), run) for path, run in _read_routes())
WRITE_ROUTES = _write_routes()
