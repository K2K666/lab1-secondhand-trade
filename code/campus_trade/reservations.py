"""Reservation lifecycle and request receipts within the caller's write transaction."""

import hashlib
import json

from .core import ApiError, audit, integer, notify, require_admin, require_member, text

PENDING_TTL = 24 * 60 * 60
CONFIRMED_TTL = 10 * 24 * 60 * 60
PAGE_SIZE = 20
ACTIVE_STATUSES = ("pending", "confirmed")
STATUSES = (*ACTIVE_STATUSES, "canceled", "rejected", "completed")
MANUAL_ACTIONS = {"confirm", "reject", "cancel", "complete"}

_JOIN = """
    FROM reservation AS r
    JOIN product AS p ON p.id = r.product_id
    JOIN account AS buyer ON buyer.id = r.buyer_id
    JOIN account AS seller ON seller.id = p.seller_id
"""
_SELECT = (
    """
    SELECT r.*, p.seller_id, p.status AS product_status,
           p.version AS product_version, p.hidden AS product_hidden,
           buyer.nickname AS buyer_name, seller.nickname AS seller_name
"""
    + _JOIN
)


def _booking(db, reservation_id):
    row = db.execute(_SELECT + " WHERE r.id = ?", (reservation_id,)).fetchone()
    if row is None:
        raise ApiError(404, "预约不存在", "reservation_not_found")
    return row


def _response(row):
    result = dict(row)
    result.pop("product_hidden")
    result["completed_by"] = row["ended_by"] if row["status"] == "completed" else None
    return result


def _receipt(db, actor_id, action, request_key, parameters):
    key = text(request_key, "请求标识", 128)
    payload = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    receipt_id = (actor_id, action, key)
    previous = db.execute(
        "SELECT digest, result_json FROM request_result "
        "WHERE account_id = ? AND action = ? AND request_key = ?",
        receipt_id,
    ).fetchone()
    if previous is None:
        return receipt_id, digest, None
    if previous["digest"] != digest:
        raise ApiError(409, "请求标识已用于不同的操作参数", "request_key_conflict")
    return receipt_id, digest, json.loads(previous["result_json"])


def _remember(db, ctx, receipt_id, digest, result):
    db.execute(
        "INSERT INTO request_result(account_id, action, request_key, digest, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (*receipt_id, digest, json.dumps(result, ensure_ascii=False), ctx.now),
    )


def _change_product(db, product_id, before, after, version, now):
    updated = db.execute(
        "UPDATE product SET status = ?, version = version + 1, updated_at = ?, "
        "sold_at = CASE WHEN ? = 'sold' THEN ? ELSE sold_at END "
        "WHERE id = ? AND status = ? AND version = ? AND hidden = 0",
        (after, now, after, now, product_id, before, version),
    )
    if updated.rowcount != 1:
        raise ApiError(409, "商品状态已更新，请刷新页面后再操作", "product_changed")


def _audit_booking(db, ctx, action, reservation_id, product_id, before, after, detail=None):
    audit(
        db,
        ctx,
        action,
        object_type="reservation",
        object_id=reservation_id,
        product_id=product_id,
        before=before,
        after=after,
        detail=detail,
    )


def create_reservation(db, ctx, product_id, data):
    """Reserve a published listing at the version displayed to the buyer."""
    buyer_id = require_member(ctx)
    product_id = integer(product_id, "商品编号")
    version = integer(data.get("version"), "商品版本")
    product = db.execute(
        "SELECT id, seller_id, title, price_cents, location, status, version, hidden, "
        "first_published_at FROM product WHERE id = ?",
        (product_id,),
    ).fetchone()
    if product is None:
        raise ApiError(404, "商品不存在", "product_not_found")
    if product["seller_id"] == buyer_id:
        raise ApiError(403, "无法预约自己发布的商品", "own_product")

    action = "reservation.create"
    receipt_id, digest, previous = _receipt(
        db,
        buyer_id,
        action,
        data.get("request_key"),
        {"product_id": product_id, "version": version},
    )
    if previous is not None:
        return previous

    if product["first_published_at"] is None and product["status"] not in (
        "published",
        "reserved",
        "sold",
    ):
        raise ApiError(404, "商品不存在", "product_not_found")
    active = db.execute(
        "SELECT buyer_id FROM reservation WHERE product_id = ? AND status IN ('pending', 'confirmed')",
        (product_id,),
    ).fetchone()
    if active is not None and active["buyer_id"] == buyer_id:
        raise ApiError(409, "您已预约该商品，请勿重复操作", "already_reserved")
    if (
        product["version"] != version
        or product["status"] != "published"
        or product["hidden"]
        or active
    ):
        raise ApiError(409, "商品状态已更新，请刷新页面后再操作", "product_changed")

    now = ctx.now
    cursor = db.execute(
        "INSERT INTO reservation(product_id, buyer_id, product_title, price_cents, location, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (product_id, buyer_id, product["title"], product["price_cents"], product["location"], now),
    )
    reservation_id = cursor.lastrowid
    _change_product(db, product_id, "published", "reserved", version, now)
    _audit_booking(db, ctx, action, reservation_id, product_id, None, "pending")
    result = _response(_booking(db, reservation_id))
    _remember(db, ctx, receipt_id, digest, result)
    return result


def list_reservations(db, ctx, params):
    """List reservations visible to the buyer, seller, or an administrator."""
    scope = params.get("scope", "buyer")
    filters = []
    values = []
    if scope == "admin":
        require_admin(ctx)
    elif scope in ("buyer", "seller"):
        account_id = require_member(ctx)
        filters.append("r.buyer_id = ?" if scope == "buyer" else "p.seller_id = ?")
        values.append(account_id)
    else:
        raise ApiError(400, "预约查询范围无效")

    status = params.get("status", "")
    if status:
        if status not in STATUSES:
            raise ApiError(400, "预约状态无效")
        filters.append("r.status = ?")
        values.append(status)
    page = integer(params.get("page", 1), "页码")
    where = " WHERE " + " AND ".join(filters) if filters else ""
    total = db.execute("SELECT COUNT(*)" + _JOIN + where, values).fetchone()[0]
    rows = db.execute(
        _SELECT + where + " ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
        (*values, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    return {
        "items": [_response(row) for row in rows],
        "page": page,
        "total": total,
        "page_size": PAGE_SIZE,
    }


def _transition(db, ctx, row, after, action, *, reason="", product_after=None):
    """Apply one guarded transition. Violation leaves the product write to its caller."""
    before = row["status"]
    if (
        before not in ACTIVE_STATUSES
        or row["product_status"] != "reserved"
        or row["product_hidden"]
    ):
        raise ApiError(409, "预约状态已更新，请刷新页面后再操作", "reservation_changed")

    now = ctx.now
    terminal = after not in ACTIVE_STATUSES
    changed = db.execute(
        "UPDATE reservation SET status = ?, confirmed_at = ?, ended_at = ?, ended_by = ?, reason = ? "
        "WHERE id = ? AND status = ?",
        (
            after,
            now if after == "confirmed" else row["confirmed_at"],
            now if terminal else None,
            ctx.account_id if terminal else None,
            reason,
            row["id"],
            before,
        ),
    )
    if changed.rowcount != 1:
        raise ApiError(409, "预约状态已更新，请刷新页面后再操作", "reservation_changed")
    if product_after is not None:
        _change_product(
            db, row["product_id"], "reserved", product_after, row["product_version"], now
        )
    _audit_booking(db, ctx, action, row["id"], row["product_id"], before, after, reason or None)


def _notify_booking(db, ctx, row, event, recipients, message):
    key = f"reservation:{row['id']}:{event}"
    for recipient_id in recipients:
        notify(db, recipient_id, key, f"《{row['product_title']}》{message}", ctx.now)


def _confirm(db, ctx, row, audit_action):
    if row["status"] == "canceled" and row["ended_by"] == row["buyer_id"]:
        raise ApiError(409, "该预约已被买家取消", "buyer_canceled")
    if row["status"] != "pending":
        raise ApiError(409, "只有待卖家确认的预约可以确认", "reservation_changed")
    _transition(db, ctx, row, "confirmed", audit_action, product_after="reserved")


def _reject(db, ctx, row, audit_action, reason):
    if row["status"] != "pending":
        raise ApiError(409, "只有待卖家确认的预约可以拒绝", "reservation_changed")
    _transition(db, ctx, row, "rejected", audit_action, reason=reason, product_after="published")
    message = f"的预约已被卖家拒绝。原因：{reason}" if reason else "的预约已被卖家拒绝。"
    _notify_booking(db, ctx, row, "rejected", (row["buyer_id"],), message)


def _cancel(db, ctx, row, audit_action, actor_id, reason):
    if row["status"] == "pending" and actor_id == row["buyer_id"]:
        reason = reason or "买家取消"
    elif row["status"] == "confirmed" and actor_id == row["seller_id"]:
        reason = text(reason, "取消原因")
    elif row["status"] == "confirmed":
        raise ApiError(409, "卖家已确认预约，无法单方面取消，请联系卖家协商", "seller_confirmed")
    elif row["status"] == "pending":
        raise ApiError(409, "卖家可拒绝待确认预约", "reservation_changed")
    else:
        raise ApiError(409, "该预约已结束，无法取消", "reservation_changed")
    _transition(db, ctx, row, "canceled", audit_action, reason=reason, product_after="published")


def _complete(db, ctx, row, audit_action):
    if row["status"] != "confirmed":
        raise ApiError(409, "只有已确认且未结束的预约可以标记完成", "reservation_changed")
    _transition(db, ctx, row, "completed", audit_action, product_after="sold")


def reservation_action(db, ctx, reservation_id, action, data):
    """Act on a reservation as its buyer or seller and replay matching successful requests."""
    actor_id = require_member(ctx)
    if action not in MANUAL_ACTIONS:
        raise ApiError(400, "预约操作无效")
    reservation_id = integer(reservation_id, "预约编号")
    row = _booking(db, reservation_id)
    if actor_id not in (row["buyer_id"], row["seller_id"]):
        raise ApiError(403, "无权操作此预约", "forbidden")
    if action in ("confirm", "reject") and actor_id != row["seller_id"]:
        raise ApiError(403, "仅对应卖家可以处理该预约", "forbidden")

    reason = text(data.get("reason"), "原因", required=False)
    audit_action = f"reservation.{action}"
    receipt_id, digest, previous = _receipt(
        db,
        actor_id,
        audit_action,
        data.get("request_key"),
        {"reservation_id": reservation_id, "reason": reason},
    )
    if previous is not None:
        return previous

    if action == "confirm":
        _confirm(db, ctx, row, audit_action)
    elif action == "reject":
        _reject(db, ctx, row, audit_action, reason)
    elif action == "cancel":
        _cancel(db, ctx, row, audit_action, actor_id, reason)
    else:
        _complete(db, ctx, row, audit_action)
    result = _response(_booking(db, reservation_id))
    _remember(db, ctx, receipt_id, digest, result)
    return result


def expire_due(db, ctx):
    """Cancel stale pending requests and complete confirmed requests past their deadline."""
    now = ctx.now
    rows = db.execute(
        _SELECT + " WHERE (r.status = 'pending' AND r.created_at <= ?) "
        "OR (r.status = 'confirmed' AND r.confirmed_at <= ?) "
        "ORDER BY r.created_at, r.id",
        (now - PENDING_TTL, now - CONFIRMED_TTL),
    ).fetchall()
    for row in rows:
        if row["status"] == "pending":
            _transition(
                db,
                ctx,
                row,
                "canceled",
                "reservation.timeout_cancel",
                reason="卖家未处理，预约已满24小时",
                product_after="published",
            )
            _notify_booking(
                db,
                ctx,
                row,
                "timeout_cancel",
                (row["buyer_id"], row["seller_id"]),
                "的预约已满24小时未处理，系统已自动取消。",
            )
        else:
            _complete(db, ctx, row, "reservation.auto_complete")
    return len(rows)


def invalidate_for_violation(db, ctx, product_id, reason):
    """Cancel the active reservation before the product service marks the violation."""
    require_admin(ctx)
    product_id = integer(product_id, "商品编号")
    reason = text(reason, "违规原因")
    row = db.execute(
        _SELECT + " WHERE r.product_id = ? AND r.status IN ('pending', 'confirmed')",
        (product_id,),
    ).fetchone()
    if row is None:
        return 0
    _transition(db, ctx, row, "canceled", "reservation.violation_cancel", reason=reason)
    _notify_booking(
        db,
        ctx,
        row,
        "violation_cancel",
        (row["buyer_id"], row["seller_id"]),
        f"的预约因商品违规已取消。原因：{reason}",
    )
    return 1
