"""Read models for statistics, notifications, and operation history."""

from datetime import date, datetime, time, timedelta, timezone

from .core import ApiError, integer, require_admin, require_login, require_member, text

BEIJING = timezone(timedelta(hours=8))
PAGE_SIZE = 20


def _date_filter(value, label):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError
    except (ValueError, TypeError) as error:
        raise ApiError(400, f"{label}请使用 YYYY-MM-DD") from error
    if not 1970 <= day.year <= 9998:
        raise ApiError(400, f"{label}超出范围")
    return day


def _start_of(day):
    return int(datetime.combine(day, time.min, BEIJING).timestamp())


def list_logs(db, ctx, params):
    scope = params.get("scope", "mine")
    clauses = []
    args = []
    if scope == "mine":
        clauses.append("actor_id = ?")
        args.append(require_login(ctx))
        if params.get("actor_id"):
            raise ApiError(403, "个人历史只能查看本人记录", "forbidden")
    elif scope == "admin":
        require_admin(ctx)
        if params.get("actor_id"):
            clauses.append("actor_id = ?")
            args.append(integer(params["actor_id"], "操作者编号"))
    else:
        raise ApiError(400, "未知日志范围")

    start = _date_filter(params["start"], "起始日期") if params.get("start") else None
    end = _date_filter(params["end"], "结束日期") if params.get("end") else None
    if start and end and start > end:
        raise ApiError(400, "起始日期不能晚于结束日期")
    if start:
        clauses.append("created_at >= ?")
        args.append(_start_of(start))
    if end:
        clauses.append("created_at < ?")
        args.append(_start_of(end + timedelta(days=1)))
    if params.get("action"):
        clauses.append("action = ?")
        args.append(text(params["action"], "操作类型", 80))
    if params.get("product_id"):
        clauses.append("product_id = ?")
        args.append(integer(params["product_id"], "商品编号"))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    page = integer(params.get("page", 1), "页码")
    total = db.execute("SELECT COUNT(*) FROM audit_log" + where, args).fetchone()[0]
    rows = db.execute(
        """
        SELECT id, action, object_type, object_id, product_id, actor_id,
               before, after, success, detail, ip, created_at
        FROM audit_log
        """
        + where
        + " ORDER BY id DESC LIMIT ? OFFSET ?",
        [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    items = []
    for row in rows:
        item = dict(row)
        item["success"] = bool(item["success"])
        if scope == "mine":
            del item["ip"]
        items.append(item)
    return {"items": items, "page": page, "total": total, "page_size": PAGE_SIZE}


def list_notifications(db, ctx):
    account_id = require_login(ctx)
    rows = db.execute(
        """
        SELECT id, message, created_at, read_at FROM notification
        WHERE recipient_id = ? ORDER BY id DESC
        """,
        (account_id,),
    )
    return {
        "items": [
            {
                "id": row["id"],
                "message": row["message"],
                "created_at": row["created_at"],
                "read": row["read_at"] is not None,
            }
            for row in rows
        ]
    }


def mark_read(db, ctx, notification_id):
    account_id = require_login(ctx)
    result = db.execute(
        """
        UPDATE notification SET read_at = COALESCE(read_at, ?)
        WHERE id = ? AND recipient_id = ?
        """,
        (ctx.now, notification_id, account_id),
    )
    if result.rowcount != 1:
        raise ApiError(404, "通知不存在", "not_found")
    return {"id": notification_id, "read": True}


def stats(db, ctx, params):
    scope = params.get("scope", "mine")
    if scope == "mine":
        return _seller_stats(db, require_member(ctx))
    if scope == "admin":
        require_admin(ctx)
        return _platform_stats(db, ctx.now)
    raise ApiError(400, "未知统计范围")


def _seller_stats(db, account_id):
    published = db.execute(
        "SELECT COUNT(*) FROM product WHERE seller_id = ? AND first_published_at IS NOT NULL",
        (account_id,),
    ).fetchone()[0]
    activity = db.execute(
        """
        SELECT COUNT(*) AS reservations,
               COUNT(CASE WHEN r.status = 'completed' THEN 1 END) AS completed
        FROM reservation AS r JOIN product AS p ON p.id = r.product_id
        WHERE p.seller_id = ?
        """,
        (account_id,),
    ).fetchone()
    return {"scope": "mine", "published": published, **dict(activity)}


def _activity_by_date(db, since):
    # Group at the database layer so the seven-day view does not load old rows.
    rows = db.execute(
        """
        SELECT date(created_at, 'unixepoch', '+8 hours') AS day,
               COUNT(*) AS count FROM reservation
        WHERE created_at >= ? GROUP BY day
        """,
        (since,),
    )
    reservations = {row["day"]: row["count"] for row in rows}
    rows = db.execute(
        """
        SELECT date(ended_at, 'unixepoch', '+8 hours') AS day,
               COUNT(*) AS count FROM reservation
        WHERE status = 'completed' AND ended_at >= ? GROUP BY day
        """,
        (since,),
    )
    return reservations, {row["day"]: row["count"] for row in rows}


def _platform_stats(db, timestamp):
    today = datetime.fromtimestamp(timestamp, BEIJING).date()
    first = today - timedelta(days=6)
    reservations, completed = _activity_by_date(db, _start_of(first))
    daily = []
    for offset in range(7):
        day = (first + timedelta(days=offset)).isoformat()
        daily.append(
            {
                "date": day,
                "reservations": reservations.get(day, 0),
                "completed": completed.get(day, 0),
            }
        )

    categories = db.execute(
        """
        SELECT COALESCE(c.name, '未分类') AS name, COUNT(*) AS count
        FROM product p LEFT JOIN category c ON p.category_id = c.id
        GROUP BY p.category_id ORDER BY count DESC, name
        """,
    )
    statuses = db.execute(
        "SELECT status, COUNT(*) AS count FROM product GROUP BY status ORDER BY status",
    )
    violations = db.execute(
        """
        SELECT a.id AS account_id, a.nickname, COUNT(*) AS count
        FROM violation v JOIN product p ON v.product_id = p.id
        JOIN account a ON p.seller_id = a.id
        GROUP BY a.id ORDER BY count DESC, a.id
        """,
    )
    return {
        "scope": "admin",
        "total_products": db.execute("SELECT COUNT(*) FROM product").fetchone()[0],
        "categories": [dict(row) for row in categories],
        "statuses": [dict(row) for row in statuses],
        "daily": daily,
        "violations": [dict(row) for row in violations],
    }
