"""Local account lifecycle, password storage, and session authentication."""

import hashlib
import hmac
import re
import secrets
import sqlite3

from .core import (
    ApiError,
    Context,
    audit,
    require_login,
    require_super,
    text,
)

SESSION_SECONDS = 12 * 60 * 60
PASSWORD_ROUNDS = 350_000
CODE_ROUNDS = 45_000
PHONE_PATTERN = re.compile(r"1[0-9]{10}")


def _digest(value: str, salt: bytes, rounds: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", value.encode(), salt, rounds).hex()


def _hash_secret(value: str, rounds: int) -> str:
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}:{_digest(value, salt, rounds)}"


def _matches(value: str, stored: str, rounds: int) -> bool:
    salt, expected = stored.split(":", 1)
    return hmac.compare_digest(expected, _digest(value, bytes.fromhex(salt), rounds))


def _phone(value) -> str:
    phone = text(value, "手机号", 11)
    if not PHONE_PATTERN.fullmatch(phone):
        raise ApiError(400, "请输入 11 位手机号")
    return phone


def _password(value, label="密码", *, new=False) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ApiError(400, f"{label}格式无效")
    if new and (len(value) < 8 or not value.strip()):
        raise ApiError(400, f"{label}至少 8 个字符")
    return value


def _avatar(value) -> str:
    # The prototype stores a short display label instead of fetching an external URL.
    return text(value, "头像标记", 12, required=False)


def public_account(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "phone": row["phone"],
        "nickname": row["nickname"],
        "role": row["role"],
        "must_change": bool(row["must_change"]),
        "avatar": row["avatar"],
        "bio": row["bio"],
    }


def _load_account(db, account_id: int):
    return db.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def context_for(db, token: str | None, ip: str, now: int) -> Context:
    account = None
    if token and len(token) <= 80:
        account = db.execute(
            """
            SELECT account.id, account.role, account.must_change
            FROM session JOIN account ON account.id = session.account_id
            WHERE session.token_hash = ? AND session.expires_at > ?
            """,
            (_token_hash(token), now),
        ).fetchone()
    if account is None:
        return Context(ip=ip, timestamp=now)
    return Context(
        account["id"],
        account["role"],
        ip,
        bool(account["must_change"]),
        now,
    )


def whoami(db, ctx):
    account = _load_account(db, ctx.account_id) if ctx.account_id is not None else None
    categories = db.execute("SELECT id, name FROM category ORDER BY id")
    return {"account": public_account(account), "categories": [dict(row) for row in categories]}


def issue_code(db, ctx, data):
    phone = _phone(data.get("phone"))
    row = db.execute(
        "SELECT issued_at FROM verification_code WHERE phone = ?",
        (phone,),
    ).fetchone()
    if row and ctx.now - row["issued_at"] < 30:
        raise ApiError(429, "请等 30 秒后再次获取验证码", "rate_limited")
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.execute(
        """
        INSERT INTO verification_code (phone, code_hash, issued_at, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET code_hash = excluded.code_hash,
            issued_at = excluded.issued_at, expires_at = excluded.expires_at
        """,
        (phone, _hash_secret(code, CODE_ROUNDS), ctx.now, ctx.now + 300),
    )
    return {"message": "本地演示验证码已生成", "development_code": code}


def _insert_account(db, data, role, now, *, must_change=False):
    phone = _phone(data.get("phone"))
    nickname = text(data.get("nickname"), "昵称", 32)
    password = _password(data.get("password"), new=True)
    if db.execute("SELECT 1 FROM account WHERE phone = ?", (phone,)).fetchone():
        raise ApiError(409, "该手机号已注册", "phone_exists")
    try:
        cursor = db.execute(
            """
            INSERT INTO account (
                phone, nickname, password_hash, role, must_change, avatar, bio, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                phone,
                nickname,
                _hash_secret(password, PASSWORD_ROUNDS),
                role,
                int(must_change),
                _avatar(data.get("avatar")),
                text(data.get("bio"), "个人简介", 300, required=False),
                now,
            ),
        )
    except sqlite3.IntegrityError as error:
        if role == "super":
            raise ApiError(409, "已存在超级管理员", "super_exists") from error
        raise
    return _load_account(db, cursor.lastrowid)


def register(db, ctx, data):
    phone = _phone(data.get("phone"))
    code = text(data.get("code"), "验证码", 6)
    row = db.execute(
        "SELECT code_hash, expires_at FROM verification_code WHERE phone = ?",
        (phone,),
    ).fetchone()
    if (
        not row
        or row["expires_at"] <= ctx.now
        or not _matches(code, row["code_hash"], CODE_ROUNDS)
    ):
        raise ApiError(400, "验证码错误或已过期", "invalid_code")
    account = _insert_account(db, data, "member", ctx.now)
    db.execute("DELETE FROM verification_code WHERE phone = ?", (phone,))
    actor = Context(account["id"], "member", ctx.ip, timestamp=ctx.now)
    audit(db, actor, "register", object_type="account", object_id=account["id"])
    return {"message": "注册成功，请登录"}


def _create_session(db, account_id: int, now: int) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM session WHERE expires_at <= ?", (now,))
    db.execute(
        "INSERT INTO session (token_hash, account_id, expires_at) VALUES (?, ?, ?)",
        (_token_hash(token), account_id, now + SESSION_SECONDS),
    )
    return token


def login(db, ctx, data, previous_token):
    phone = _phone(data.get("phone"))
    password = _password(data.get("password"))
    account = db.execute("SELECT * FROM account WHERE phone = ?", (phone,)).fetchone()
    if account is None:
        _digest(password, bytes(16), PASSWORD_ROUNDS)
        raise ApiError(401, "手机号或密码错误", "invalid_credentials")
    if not _matches(password, account["password_hash"], PASSWORD_ROUNDS):
        raise ApiError(401, "手机号或密码错误", "invalid_credentials")
    logout(db, previous_token)
    token = _create_session(db, account["id"], ctx.now)
    actor = Context(account["id"], account["role"], ctx.ip, timestamp=ctx.now)
    audit(db, actor, "login", object_type="account", object_id=account["id"])
    return {"account": public_account(account)}, token


def logout(db, token):
    if token:
        db.execute("DELETE FROM session WHERE token_hash = ?", (_token_hash(token),))
    return {"message": "已退出登录"}


def change_password(db, ctx, data):
    if ctx.account_id is None:
        raise ApiError(401, "请先登录", "login_required")
    previous = _password(data.get("old_password"), "原密码")
    replacement = _password(data.get("new_password"), "新密码", new=True)
    if replacement == previous:
        raise ApiError(400, "新密码须与原密码不同")
    account = _load_account(db, ctx.account_id)
    if not _matches(previous, account["password_hash"], PASSWORD_ROUNDS):
        raise ApiError(400, "原密码不正确", "invalid_credentials")
    db.execute(
        "UPDATE account SET password_hash = ?, must_change = 0 WHERE id = ?",
        (_hash_secret(replacement, PASSWORD_ROUNDS), ctx.account_id),
    )
    db.execute("DELETE FROM session WHERE account_id = ?", (ctx.account_id,))
    token = _create_session(db, ctx.account_id, ctx.now)
    audit(db, ctx, "change_password", object_type="account", object_id=ctx.account_id)
    return {"account": public_account(_load_account(db, ctx.account_id))}, token


def update_profile(db, ctx, data):
    account_id = require_login(ctx)
    account = _load_account(db, account_id)
    db.execute(
        "UPDATE account SET nickname = ?, avatar = ?, bio = ? WHERE id = ?",
        (
            text(data.get("nickname", account["nickname"]), "昵称", 32),
            _avatar(data.get("avatar", account["avatar"])),
            text(data.get("bio", account["bio"]), "个人简介", 300, required=False),
            account_id,
        ),
    )
    audit(db, ctx, "update_profile", object_type="account", object_id=account_id)
    return {"account": public_account(_load_account(db, account_id))}


def create_admin(db, ctx, data):
    require_super(ctx)
    account = _insert_account(db, data, "admin", ctx.now, must_change=True)
    audit(db, ctx, "create_admin", object_type="account", object_id=account["id"])
    return {"account": public_account(account)}


def list_admins(db, ctx):
    require_super(ctx)
    rows = db.execute("SELECT * FROM account WHERE role IN ('admin', 'super') ORDER BY id")
    return {"items": [public_account(row) for row in rows]}


def bootstrap_super(db, phone, nickname, password):
    ctx = Context()
    account = _insert_account(
        db,
        {"phone": phone, "nickname": nickname, "password": password},
        "super",
        ctx.now,
    )
    audit(db, ctx, "bootstrap_super", object_type="account", object_id=account["id"])
    return public_account(account)
