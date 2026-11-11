"""Shared domain primitives and transaction boundaries."""

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class ApiError(Exception):
    """A user-visible error with a stable code."""

    def __init__(self, status: int, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Context:
    account_id: int | None = None
    role: str = "guest"
    ip: str = "127.0.0.1"
    must_change: bool = False
    timestamp: int | None = None

    @property
    def now(self) -> int:
        return int(time.time()) if self.timestamp is None else self.timestamp


def require_login(ctx: Context) -> int:
    if ctx.account_id is None:
        raise ApiError(401, "请先登录", "login_required")
    if ctx.must_change:
        raise ApiError(403, "请先修改初始密码", "password_change_required")
    return ctx.account_id


def require_member(ctx: Context) -> int:
    actor = require_login(ctx)
    if ctx.role != "member":
        raise ApiError(403, "仅普通账号可执行此操作", "forbidden")
    return actor


def require_admin(ctx: Context) -> int:
    actor = require_login(ctx)
    if ctx.role not in {"admin", "super"}:
        raise ApiError(403, "此操作需要管理员权限", "forbidden")
    return actor


def require_super(ctx: Context) -> int:
    actor = require_admin(ctx)
    if ctx.role != "super":
        raise ApiError(403, "此操作需要超级管理员权限", "forbidden")
    return actor


def text(value, label: str, maximum: int = 500, *, required=True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"{label}必须为文本")
    value = value.strip()
    if required and not value:
        raise ApiError(400, f"请输入{label}")
    if len(value) > maximum or "\x00" in value:
        raise ApiError(400, f"{label}格式无效或超过 {maximum} 字")
    return value


def integer(value, label: str, *, minimum=1, maximum=2_147_483_647) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and len(value) <= 12 and value.isascii() and value.isdecimal():
        number = int(value)
    else:
        raise ApiError(400, f"{label}必须为整数")
    if not minimum <= number <= maximum:
        raise ApiError(400, f"{label}超出范围")
    return number


def audit(
    db,
    ctx,
    action,
    *,
    object_type=None,
    object_id=None,
    product_id=None,
    before=None,
    after=None,
    success=True,
    detail=None,
):
    db.execute(
        """
        INSERT INTO audit_log (
            actor_id, action, object_type, object_id, product_id, before, after,
            success, detail, ip, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.account_id,
            action,
            object_type,
            object_id,
            product_id,
            before,
            after,
            int(success),
            detail,
            ctx.ip,
            ctx.now,
        ),
    )


def notify(db, recipient_id, event_key, message, timestamp):
    db.execute(
        """
        INSERT INTO notification (recipient_id, event_key, message, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(recipient_id, event_key) DO NOTHING
        """,
        (recipient_id, event_key, message, timestamp),
    )


def connect(path: Path | str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=8, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    schemas = ("core_schema.sql", "product_schema.sql", "reservation_schema.sql")
    with database(path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        for name in schemas:
            db.executescript(Path(__file__).with_name(name).read_text(encoding="utf-8-sig"))


@contextmanager
def database(path):
    db = connect(path)
    try:
        yield db
    finally:
        db.close()


@contextmanager
def write_transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


class WriteQueue:
    """Admit writes in the order complete requests reach the application."""

    def __init__(self):
        self._condition = threading.Condition()
        self._assigned = 0
        self._serving = 0

    @contextmanager
    def turn(self):
        with self._condition:
            ticket = self._assigned
            self._assigned += 1
            self._condition.wait_for(lambda: self._serving == ticket)
        try:
            yield ticket + 1
        finally:
            with self._condition:
                self._serving += 1
                self._condition.notify_all()
