import unittest
from pathlib import Path

from campus_trade import products, reservations
from campus_trade.core import ApiError, Context, connect, write_transaction

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jLykAAAAASUVORK5CYII="
)
SELLER = Context(1, "member", timestamp=1_800_000_000)
BUYER = Context(2, "member", timestamp=1_800_000_001)
ADMIN = Context(3, "admin", timestamp=1_800_000_002)
GUEST = Context(timestamp=1_800_000_003)
DETAILS = {
    "title": "课程教材 100%",
    "description": "概率统计",
    "category_id": 1,
    "price_cents": 1200,
    "location": "图书馆",
    "photos": [{"mime": "image/png", "data": PNG}],
}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        root = Path(products.__file__).parent
        for name in ("core_schema.sql", "product_schema.sql", "reservation_schema.sql"):
            self.db.executescript((root / name).read_text(encoding="utf-8"))
        self.db.executemany(
            "INSERT INTO account (id, phone, nickname, password_hash, role, created_at) "
            "VALUES (?, ?, ?, 'test-hash', ?, 0)",
            [(1, "1", "卖家", "member"), (2, "2", "买家", "member"), (3, "3", "审核员", "admin")],
        )

    def write(self, operation, context, *args):
        with write_transaction(self.db):
            return operation(self.db, context, *args)

    def publish(self, draft):
        self.write(products.submit_product, SELLER, draft["id"], {"version": draft["version"]})
        review = products.review_queue(self.db, ADMIN, {})["items"][0]
        return self.write(products.review_product, ADMIN, review["id"], {"decision": "approve"})

    def test_draft_and_reapproval_keep_photos_private_until_published(self):
        draft = self.write(products.create_draft, SELLER, {"title": DETAILS["title"]})
        with self.assertRaises(ApiError) as missing:
            self.write(products.submit_product, SELLER, draft["id"], {"version": draft["version"]})
        self.assertEqual(missing.exception.status, 400)
        with self.assertRaises(ApiError) as private:
            products.product_detail(self.db, BUYER, draft["id"])
        self.assertEqual(private.exception.status, 404)
        draft = self.write(
            products.edit_product, SELLER, draft["id"], {**DETAILS, "version": draft["version"]}
        )
        published = self.publish(draft)
        photo_id = published["photos"][0]["id"]
        self.assertEqual(products.get_photo(self.db, GUEST, photo_id)[0], "image/png")
        found = products.list_products(
            self.db, GUEST, {"q": "100%", "category_id": 1, "maximum": 1200}
        )
        self.assertEqual([item["id"] for item in found["items"]], [draft["id"]])
        self.assertEqual(products.list_products(self.db, GUEST, {"q": "_"})["total"], 0)
        changed = self.write(
            products.edit_product,
            SELLER,
            draft["id"],
            {
                "location": "教学楼",
                "version": published["version"],
            },
        )
        self.assertEqual(changed["status"], "published")
        recheck = self.write(
            products.edit_product,
            SELLER,
            draft["id"],
            {
                "price_cents": 1400,
                "version": changed["version"],
            },
        )
        self.assertEqual(recheck["status"], "pending_review")
        with self.assertRaises(ApiError) as denied:
            products.get_photo(self.db, GUEST, photo_id)
        self.assertEqual(denied.exception.status, 404)
        self.assertEqual(products.get_photo(self.db, SELLER, photo_id)[0], "image/png")

    def test_violation_cancels_active_reservation_and_appeal_keeps_history(self):
        published = self.publish(self.write(products.create_draft, SELLER, DETAILS))
        product_id = published["id"]
        booking = self.write(
            reservations.create_reservation,
            BUYER,
            product_id,
            {
                "version": published["version"],
                "request_key": "test-booking",
            },
        )
        reserved = products.product_detail(self.db, SELLER, product_id)
        with self.assertRaises(ApiError) as active:
            self.write(
                products.downlist_product, SELLER, product_id, {"version": reserved["version"]}
            )
        self.assertEqual(active.exception.code, "active_reservation")
        report = self.write(
            products.report_product, BUYER, product_id, {"reason": "图片与实际商品不符"}
        )
        self.write(
            products.handle_report,
            ADMIN,
            report["id"],
            {"decision": "violate", "reason": "图片违规"},
        )
        blocked = products.product_detail(self.db, SELLER, product_id)
        self.assertEqual(blocked["status"], "violation")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 2)
        self.write(
            products.submit_appeal,
            SELLER,
            product_id,
            {"version": blocked["version"], "reason": "已整改"},
        )
        review = products.review_queue(self.db, ADMIN, {"kind": "appeal"})["items"][0]
        restored = self.write(
            products.review_product, ADMIN, review["id"], {"decision": "approve"}
        )
        self.assertEqual(restored["status"], "published")
        row = self.db.execute(
            "SELECT status FROM reservation WHERE id = ?", (booking["id"],)
        ).fetchone()
        self.assertEqual(row["status"], "canceled")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM violation").fetchone()[0], 1)

    def test_sold_detail_expires_and_staff_hide_does_not_reverse_sale(self):
        published = self.publish(self.write(products.create_draft, SELLER, DETAILS))
        product_id = published["id"]
        self.db.execute(
            "UPDATE product SET status='sold', sold_at=? WHERE id=?", (BUYER.now, product_id)
        )
        self.assertEqual(products.list_products(self.db, GUEST, {})["total"], 0)
        self.assertEqual(products.product_detail(self.db, GUEST, product_id)["status"], "sold")
        past_deadline = Context(timestamp=BUYER.now + products.SOLD_VISIBILITY_SECONDS)
        with self.assertRaises(ApiError):
            products.product_detail(self.db, past_deadline, product_id)
        hidden = self.write(
            products.violate_product, ADMIN, product_id, {"reason": "后来发现违规"}
        )
        self.assertEqual(
            (hidden["status"], hidden["sold_at"], hidden["hidden"]), ("sold", BUYER.now, 1)
        )
        with self.assertRaises(ApiError):
            products.product_detail(self.db, GUEST, product_id)
