"""商品、审核与治理服务。调用方统一控制写事务和失败日志。"""

import base64
import binascii

from .core import ApiError, audit, integer, notify, require_admin, require_member, text

PRODUCT_STATES = (
    "draft",
    "pending_review",
    "published",
    "rejected",
    "downlisted",
    "reserved",
    "sold",
    "violation",
)
SUBMITTABLE = ("draft", "rejected", "downlisted")
EDITABLE = (*SUBMITTABLE, "published", "violation")
PAGE_SIZE = 20
MAX_PHOTO_BYTES = 5 * 1024 * 1024
SOLD_VISIBILITY_SECONDS = 30 * 24 * 60 * 60
SELECT_PRODUCT = """
    SELECT p.*, a.nickname AS seller_name, c.name AS category_name
      FROM product p JOIN account a ON a.id = p.seller_id
      LEFT JOIN category c ON c.id = p.category_id
"""
SORT_PRODUCTS = {
    "newest": "COALESCE(p.published_at, p.created_at) DESC, p.id DESC",
    "oldest": "COALESCE(p.published_at, p.created_at) ASC, p.id ASC",
    "price_asc": "(p.price_cents IS NULL), p.price_cents ASC, p.id ASC",
    "price_desc": "(p.price_cents IS NULL), p.price_cents DESC, p.id ASC",
}
TEXT_FIELDS = {
    "title": ("商品标题", 50),
    "description": ("商品描述", 500),
    "location": ("交易地点", 500),
    "condition": ("新旧程度", 500),
    "contact": ("联系方式", 500),
}
EDIT_FIELDS = (*TEXT_FIELDS, "category_id", "price_cents")


def _missing():
    return ApiError(404, "商品不存在或不可查看", "product_not_found")


def _product(db, product_id):
    product_id = integer(product_id, "商品编号")
    row = db.execute(SELECT_PRODUCT + " WHERE p.id = ?", (product_id,)).fetchone()
    if row is None:
        raise _missing()
    return row


def _private_access(ctx, product):
    if ctx.must_change or ctx.account_id is None:
        return False
    return ctx.role in ("admin", "super") or (
        ctx.role == "member" and ctx.account_id == product["seller_id"]
    )


def _public_detail(ctx, product):
    if product["hidden"]:
        return False
    if product["status"] == "published":
        return True
    sold_at = product["sold_at"]
    return (
        product["status"] == "sold"
        and sold_at is not None
        and sold_at <= ctx.now < sold_at + SOLD_VISIBILITY_SECONDS
    )


def _visible_product(db, ctx, product_id):
    product = _product(db, product_id)
    if not (_private_access(ctx, product) or _public_detail(ctx, product)):
        raise _missing()
    return product


def _owner(db, ctx, product_id):
    actor = require_member(ctx)
    product = _product(db, product_id)
    if actor != product["seller_id"]:
        raise ApiError(403, "只能操作自己的商品", "not_product_owner")
    return product


def _version(product, data):
    if integer(data.get("version"), "商品版本") != product["version"]:
        raise ApiError(409, "商品已更新，请刷新后重试", "stale_version")


def _without_reservation(db, product):
    active = (
        product["status"] == "reserved"
        or db.execute(
            "SELECT 1 FROM reservation WHERE product_id = ? AND status IN ('pending', 'confirmed')",
            (product["id"],),
        ).fetchone()
    )
    if active:
        raise ApiError(409, "存在有效预约，请先按预约规则处理", "active_reservation")


def _review_pending(db, product_id):
    return (
        db.execute(
            "SELECT 1 FROM product_review WHERE product_id = ? AND status = 'pending'",
            (product_id,),
        ).fetchone()
        is not None
    )


def _fields(db, data, product=None):
    if product is None:
        fields = dict.fromkeys(TEXT_FIELDS, "")
        fields.update(category_id=None, price_cents=None)
    else:
        fields = {field: product[field] for field in EDIT_FIELDS}
    for field, (label, maximum) in TEXT_FIELDS.items():
        if field in data:
            fields[field] = text(data[field], label, maximum, required=field == "title")
    if not fields["title"]:
        raise ApiError(400, "请填写商品标题")
    if "category_id" in data:
        value = data["category_id"]
        category_id = None if value is None or value == "" else integer(value, "商品分类")
        if (
            category_id is not None
            and db.execute("SELECT 1 FROM category WHERE id = ?", (category_id,)).fetchone()
            is None
        ):
            raise ApiError(400, "所选分类不存在")
        fields["category_id"] = category_id
    if "price_cents" in data:
        value = data["price_cents"]
        fields["price_cents"] = (
            None
            if value is None or value == ""
            else integer(value, "价格（分，100 至 1000000）", minimum=100, maximum=1000000)
        )
    return fields


def _photos(data):
    photos = data.get("photos", [])
    if not isinstance(photos, list) or len(photos) > 9:
        raise ApiError(400, "每件商品最多上传 9 张实物图")
    result = []
    for item in photos:
        if not isinstance(item, dict):
            raise ApiError(400, "图片资料格式错误")
        mime, encoded = item.get("mime"), item.get("data")
        if mime not in ("image/png", "image/jpeg"):
            raise ApiError(400, "图片须为 JPG 或 PNG")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PHOTO_BYTES + 2) // 3):
            raise ApiError(400, "单张图片不得超过 5 MB")
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ApiError(400, "图片内容无法读取") from None
        if len(body) > MAX_PHOTO_BYTES:
            raise ApiError(400, "单张图片不得超过 5 MB")
        if mime == "image/png":
            valid_format = body.startswith(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            ) and body.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        else:
            valid_format = body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9")
        if not valid_format:
            raise ApiError(400, "图片内容与 JPG 或 PNG 格式不符")
        result.append((mime, body))
    return result


def _replace_photos(db, product_id, photos):
    db.execute("DELETE FROM product_photo WHERE product_id = ?", (product_id,))
    db.executemany(
        "INSERT INTO product_photo (product_id, position, mime, body) VALUES (?, ?, ?, ?)",
        [(product_id, position, mime, body) for position, (mime, body) in enumerate(photos)],
    )


def _photo_count(db, product_id):
    return db.execute(
        "SELECT COUNT(*) FROM product_photo WHERE product_id = ?", (product_id,)
    ).fetchone()[0]


def _complete(fields, photo_count):
    for key, label in (
        ("title", "标题"),
        ("description", "描述"),
        ("location", "预估交易地点"),
        ("category_id", "分类"),
        ("price_cents", "价格"),
    ):
        if not fields[key]:
            raise ApiError(400, f"送审前请补齐{label}")
    if not 1 <= photo_count <= 9:
        raise ApiError(400, "送审前请上传 1 至 9 张实物图")


def _serialize(db, product, *, private=False):
    result = dict(product)
    product_id = product["id"]
    result["photos"] = [
        {"id": row["id"], "url": f"/api/photos/{row['id']}"}
        for row in db.execute(
            "SELECT id FROM product_photo WHERE product_id = ? ORDER BY position",
            (product_id,),
        )
    ]
    if private:
        review = db.execute(
            "SELECT id, kind, status, reason FROM product_review WHERE product_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        result["last_review"] = dict(review) if review else None
        violation = db.execute(
            "SELECT id, reason FROM violation WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        result["last_violation"] = dict(violation) if violation else None
    return result


def list_products(db, ctx, params):
    scope = params.get("scope") or "public"
    clauses, args = [], []
    if scope == "mine":
        clauses.append("p.seller_id = ?")
        args.append(require_member(ctx))
    elif scope == "admin":
        require_admin(ctx)
    elif scope == "public":
        clauses.append("p.status = 'published' AND p.hidden = 0")
    else:
        raise ApiError(400, "商品查询范围无效")

    keyword = text(params.get("q", ""), "关键词", 100, required=False)
    if keyword:
        pattern = "%" + keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        clauses.append("(p.title LIKE ? ESCAPE '!' OR p.description LIKE ? ESCAPE '!')")
        args.extend((pattern, pattern))
    category_id = params.get("category_id")
    if category_id not in (None, ""):
        clauses.append("p.category_id = ?")
        args.append(integer(category_id, "商品分类"))
    lower, upper = params.get("minimum"), params.get("maximum")
    low = None if lower in (None, "") else integer(lower, "最低价", minimum=0)
    high = None if upper in (None, "") else integer(upper, "最高价", minimum=0)
    if low is not None and high is not None and low > high:
        raise ApiError(400, "最低价不能高于最高价")
    for operator, value in ((">=", low), ("<=", high)):
        if value is not None:
            clauses.append(f"p.price_cents {operator} ?")
            args.append(value)
    status = params.get("status")
    if status:
        if status not in PRODUCT_STATES:
            raise ApiError(400, "商品状态无效")
        clauses.append("p.status = ?")
        args.append(status)
    sort = SORT_PRODUCTS.get(params.get("sort") or "newest")
    if sort is None:
        raise ApiError(400, "商品排序方式无效")
    page = integer(params.get("page") or 1, "页码")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = db.execute("SELECT COUNT(*) FROM product p" + where, args).fetchone()[0]
    rows = db.execute(
        SELECT_PRODUCT + where + f" ORDER BY {sort} LIMIT ? OFFSET ?",
        (*args, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return {
        "items": [_serialize(db, row, private=scope != "public") for row in rows],
        "page": page,
        "total": total,
        "page_size": PAGE_SIZE,
    }


def product_detail(db, ctx, product_id):
    product = _visible_product(db, ctx, product_id)
    return _serialize(db, product, private=_private_access(ctx, product))


def get_photo(db, ctx, photo_id):
    photo = db.execute(
        """SELECT f.mime, f.body, p.seller_id, p.status, p.hidden, p.sold_at
           FROM product_photo f JOIN product p ON p.id = f.product_id WHERE f.id = ?""",
        (integer(photo_id, "图片编号"),),
    ).fetchone()
    if photo is None or not (_private_access(ctx, photo) or _public_detail(ctx, photo)):
        raise ApiError(404, "图片不存在或不可查看")
    return photo["mime"], photo["body"]


def _submit_review(db, product_id, timestamp, kind="submission", reason=""):
    return db.execute(
        "INSERT INTO product_review (product_id, kind, request_reason, created_at) VALUES (?, ?, ?, ?)",
        (product_id, kind, reason, timestamp),
    ).lastrowid


def create_draft(db, ctx, data):
    actor = require_member(ctx)
    fields = _fields(db, data)
    photos = _photos(data)
    product_id = db.execute(
        """INSERT INTO product
           (seller_id, title, description, location, condition, contact, category_id,
            price_cents, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (actor, *(fields[key] for key in EDIT_FIELDS), ctx.now, ctx.now),
    ).lastrowid
    _replace_photos(db, product_id, photos)
    audit(
        db,
        ctx,
        "product.create",
        object_type="product",
        object_id=product_id,
        product_id=product_id,
        after="draft",
    )
    return product_detail(db, ctx, product_id)


def edit_product(db, ctx, product_id, data):
    product = _owner(db, ctx, product_id)
    product_id = product["id"]
    _version(product, data)
    _without_reservation(db, product)
    old_status = product["status"]
    if old_status not in EDITABLE or _review_pending(db, product_id):
        raise ApiError(409, "当前商品状态不能修改", "invalid_product_state")
    fields = _fields(db, data, product)
    changed = {key: fields[key] for key in EDIT_FIELDS if fields[key] != product[key]}
    photos = _photos(data) if "photos" in data else None
    if photos is not None:
        existing = db.execute(
            "SELECT mime, body FROM product_photo WHERE product_id = ? ORDER BY position",
            (product_id,),
        ).fetchall()
        if photos == [tuple(row) for row in existing]:
            photos = None
    if not changed and photos is None:
        return product_detail(db, ctx, product_id)
    needs_review = old_status == "published" and (
        photos is not None or any(key != "location" for key in changed)
    )
    status = "pending_review" if needs_review else old_status
    if needs_review:
        _complete(fields, len(photos) if photos is not None else _photo_count(db, product_id))
    db.execute(
        """UPDATE product SET title = ?, description = ?, location = ?, condition = ?,
           contact = ?, category_id = ?, price_cents = ?, status = ?, updated_at = ?,
           version = version + 1 WHERE id = ?""",
        (*(fields[key] for key in EDIT_FIELDS), status, ctx.now, product_id),
    )
    if photos is not None:
        _replace_photos(db, product_id, photos)
    if needs_review:
        _submit_review(db, product_id, ctx.now)
    changed_names = list(changed)
    if photos is not None:
        changed_names.append("photos")
    audit(
        db,
        ctx,
        "product.edit",
        object_type="product",
        object_id=product_id,
        product_id=product_id,
        before=old_status,
        after=status,
        detail="、".join(changed_names),
    )
    return product_detail(db, ctx, product_id)


def submit_product(db, ctx, product_id, data):
    product = _owner(db, ctx, product_id)
    product_id = product["id"]
    _version(product, data)
    _without_reservation(db, product)
    if product["status"] not in SUBMITTABLE or _review_pending(db, product_id):
        raise ApiError(409, "当前商品状态不能送审", "invalid_product_state")
    _complete(product, _photo_count(db, product_id))
    db.execute(
        "UPDATE product SET status = 'pending_review', version = version + 1, updated_at = ? WHERE id = ?",
        (ctx.now, product_id),
    )
    _submit_review(db, product_id, ctx.now)
    audit(
        db,
        ctx,
        "product.submit",
        object_type="product",
        object_id=product_id,
        product_id=product_id,
        before=product["status"],
        after="pending_review",
    )
    return product_detail(db, ctx, product_id)


def downlist_product(db, ctx, product_id, data):
    product = _owner(db, ctx, product_id)
    product_id = product["id"]
    _version(product, data)
    _without_reservation(db, product)
    if product["status"] != "published":
        raise ApiError(409, "只有已发布且未被预约的商品可以主动下架", "invalid_product_state")
    db.execute(
        "UPDATE product SET status = 'downlisted', version = version + 1, updated_at = ? WHERE id = ?",
        (ctx.now, product_id),
    )
    audit(
        db,
        ctx,
        "product.downlist",
        object_type="product",
        object_id=product_id,
        product_id=product_id,
        before="published",
        after="downlisted",
    )
    return product_detail(db, ctx, product_id)


def review_queue(db, ctx, params):
    require_admin(ctx)
    status = params.get("status") or "pending"
    if status not in ("all", "pending", "approved", "rejected", "closed"):
        raise ApiError(400, "审核状态无效")
    kind = params.get("kind") or "all"
    if kind not in ("all", "submission", "appeal"):
        raise ApiError(400, "审核类型无效")
    rows = db.execute(
        """SELECT r.*, p.title, p.status AS product_status FROM product_review r
           JOIN product p ON p.id = r.product_id
           WHERE (? = 'all' OR r.status = ?) AND (? = 'all' OR r.kind = ?)
           ORDER BY r.id DESC""",
        (status, status, kind, kind),
    )
    return {"items": [dict(row) for row in rows]}


def review_product(db, ctx, review_id, data):
    actor = require_admin(ctx)
    review_id = integer(review_id, "审核编号")
    review = db.execute("SELECT * FROM product_review WHERE id = ?", (review_id,)).fetchone()
    if review is None:
        raise ApiError(404, "审核记录不存在")
    if review["status"] != "pending":
        raise ApiError(409, "审核已处理", "review_already_handled")
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        raise ApiError(400, "审核结果无效")
    reason = text(data.get("reason", ""), "审核原因", required=decision == "reject")
    product = _product(db, review["product_id"])
    product_id = product["id"]
    expected = "violation" if review["kind"] == "appeal" else "pending_review"
    if product["status"] != expected:
        raise ApiError(409, "商品状态已变化", "invalid_product_state")
    _without_reservation(db, product)
    result = "approved" if decision == "approve" else "rejected"
    status = (
        "published"
        if decision == "approve"
        else ("violation" if review["kind"] == "appeal" else "rejected")
    )
    db.execute(
        """UPDATE product_review SET status = ?, reason = ?, actor_id = ?, decided_at = ?
           WHERE id = ?""",
        (result, reason, actor, ctx.now, review_id),
    )
    if decision == "approve":
        _complete(product, _photo_count(db, product_id))
        db.execute(
            """UPDATE product SET status = ?, hidden = 0, published_at = ?,
               first_published_at = COALESCE(first_published_at, ?),
               updated_at = ?, version = version + 1 WHERE id = ?""",
            (status, ctx.now, ctx.now, ctx.now, product_id),
        )
    else:
        db.execute(
            "UPDATE product SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
            (status, ctx.now, product_id),
        )
    audit(
        db,
        ctx,
        f"review.{decision}",
        object_type="product_review",
        object_id=review_id,
        product_id=product_id,
        before=expected,
        after=status,
        detail=reason,
    )
    return product_detail(db, ctx, product_id)


def report_product(db, ctx, product_id, data):
    reporter = require_member(ctx)
    product = _product(db, product_id)
    product_id = product["id"]
    if not (_private_access(ctx, product) or _public_detail(ctx, product)):
        own_booking = db.execute(
            "SELECT 1 FROM reservation WHERE product_id = ? AND buyer_id = ?",
            (product_id, reporter),
        ).fetchone()
        if own_booking is None:
            raise _missing()
    reason = text(data.get("reason", ""), "举报原因")
    report_id = db.execute(
        "INSERT INTO product_report (product_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (product_id, reporter, reason, ctx.now),
    ).lastrowid
    audit(
        db,
        ctx,
        "report.create",
        object_type="product_report",
        object_id=report_id,
        product_id=product_id,
        after="pending",
    )
    return {"id": report_id, "product_id": product_id, "status": "pending"}


def report_queue(db, ctx, params):
    require_admin(ctx)
    status = params.get("status") or "pending"
    if status not in ("all", "pending", "dismissed", "violation"):
        raise ApiError(400, "举报状态无效")
    rows = db.execute(
        """SELECT r.*, p.title FROM product_report r JOIN product p ON p.id = r.product_id
           WHERE (? = 'all' OR r.status = ?) ORDER BY r.id DESC""",
        (status, status),
    )
    return {"items": [dict(row) for row in rows]}


def _violate(db, ctx, actor, product, reason, *, reuse=False):
    from .reservations import invalidate_for_violation

    product_id, old_status = product["id"], product["status"]
    if old_status == "violation" or (old_status == "sold" and product["hidden"]):
        if reuse:
            previous = db.execute(
                "SELECT id FROM violation WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                (product_id,),
            ).fetchone()
            if previous is not None:
                return previous["id"]
        raise ApiError(409, "商品已完成违规下架处理", "violation_already_handled")
    closed_reservations = invalidate_for_violation(db, ctx, product_id, reason)
    status = "sold" if old_status == "sold" else "violation"
    violation_id = db.execute(
        "INSERT INTO violation (product_id, actor_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (product_id, actor, reason, ctx.now),
    ).lastrowid
    db.execute(
        """UPDATE product SET status = ?, hidden = 1, updated_at = ?, version = version + 1
           WHERE id = ?""",
        (status, ctx.now, product_id),
    )
    db.execute(
        """UPDATE product_review SET status = 'closed', reason = ?, actor_id = ?, decided_at = ?
           WHERE product_id = ? AND status = 'pending'""",
        (reason, actor, ctx.now, product_id),
    )
    if not closed_reservations:
        notify(
            db,
            product["seller_id"],
            f"product-violation:{violation_id}",
            f"商品「{product['title']}」因违规已隐藏，原因：{reason}",
            ctx.now,
        )
    audit(
        db,
        ctx,
        "product.violate",
        object_type="violation",
        object_id=violation_id,
        product_id=product_id,
        before=old_status,
        after=status,
        detail=reason,
    )
    return violation_id


def handle_report(db, ctx, report_id, data):
    actor = require_admin(ctx)
    report_id = integer(report_id, "举报编号")
    report = db.execute("SELECT * FROM product_report WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        raise ApiError(404, "举报记录不存在")
    if report["status"] != "pending":
        raise ApiError(409, "举报已处理", "report_already_handled")
    decision = data.get("decision")
    if decision not in ("dismiss", "violate"):
        raise ApiError(400, "举报处理结果无效")
    reason = text(data.get("reason", ""), "处理原因", required=decision == "violate")
    product_id = report["product_id"]
    violation_id = (
        _violate(db, ctx, actor, _product(db, product_id), reason, reuse=True)
        if decision == "violate"
        else None
    )
    status = "violation" if violation_id is not None else "dismissed"
    db.execute(
        """UPDATE product_report SET status = ?, actor_id = ?, decision_reason = ?,
           violation_id = ?, decided_at = ? WHERE id = ?""",
        (status, actor, reason, violation_id, ctx.now, report_id),
    )
    audit(
        db,
        ctx,
        f"report.{decision}",
        object_type="product_report",
        object_id=report_id,
        product_id=product_id,
        before="pending",
        after=status,
        detail=reason,
    )
    return {
        "id": report_id,
        "product_id": product_id,
        "status": status,
        "violation_id": violation_id,
    }


def violate_product(db, ctx, product_id, data):
    actor = require_admin(ctx)
    product = _product(db, product_id)
    reason = text(data.get("reason", ""), "违规原因")
    _violate(db, ctx, actor, product, reason)
    return product_detail(db, ctx, product["id"])


def submit_appeal(db, ctx, product_id, data):
    product = _owner(db, ctx, product_id)
    product_id = product["id"]
    _version(product, data)
    if product["status"] != "violation" or _review_pending(db, product_id):
        raise ApiError(409, "当前商品无法提交整改申诉", "invalid_product_state")
    reason = text(data.get("reason", ""), "整改说明")
    _complete(product, _photo_count(db, product_id))
    review_id = _submit_review(db, product_id, ctx.now, "appeal", reason)
    db.execute(
        "UPDATE product SET version = version + 1, updated_at = ? WHERE id = ?",
        (ctx.now, product_id),
    )
    audit(
        db,
        ctx,
        "appeal.submit",
        object_type="product_review",
        object_id=review_id,
        product_id=product_id,
        before="violation",
        after="violation",
        detail=reason,
    )
    return product_detail(db, ctx, product_id)
