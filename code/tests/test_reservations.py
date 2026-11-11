import sqlite3
import unittest
from pathlib import Path

from campus_trade import reservations as bookings
from campus_trade.core import ApiError, Context, connect, write_transaction
from campus_trade.products import violate_product


class ReservationTests(unittest.TestCase):
    start = 1_700_000_000

    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        schema_dir = Path(bookings.__file__).parent
        for name in ("core_schema.sql", "product_schema.sql", "reservation_schema.sql"):
            self.db.executescript((schema_dir / name).read_text(encoding="utf-8-sig"))
        self.db.executemany(
            "INSERT INTO account(id, phone, nickname, password_hash, role, created_at) "
            "VALUES (?, ?, ?, 'test-only', ?, ?)",
            [
                (1, "10000000001", "卖家", "member", self.start),
                (2, "10000000002", "买家甲", "member", self.start),
                (3, "10000000003", "买家乙", "member", self.start),
                (4, "10000000004", "管理员", "admin", self.start),
            ],
        )
        self.db.executemany(
            "INSERT INTO product(id, seller_id, category_id, title, description, price_cents, location, "
            '"condition", contact, status, version, created_at, updated_at, published_at, first_published_at) '
            "VALUES (?, 1, 2, ?, '实验商品', 9900, '学生中心', '九成新', '', 'published', 1, ?, ?, ?, ?)",
            [(1, "键盘", *(self.start,) * 4), (2, "台灯", *(self.start,) * 4)],
        )

    def ctx(self, actor=None, *, at=None):
        role = "guest" if actor is None else "admin" if actor == 4 else "member"
        return Context(actor, role, timestamp=self.start if at is None else at)

    def reserve(self, actor=2, product=1, version=1, key="book-1"):
        with write_transaction(self.db):
            return bookings.create_reservation(
                self.db,
                self.ctx(actor),
                product,
                {"version": version, "request_key": key},
            )

    def action(self, actor, reservation, action, key, reason=None, *, at=None):
        with write_transaction(self.db):
            return bookings.reservation_action(
                self.db,
                self.ctx(actor, at=at),
                reservation,
                action,
                {"request_key": key, "reason": reason},
            )

    def product(self, product=1):
        return self.db.execute("SELECT * FROM product WHERE id = ?", (product,)).fetchone()

    def expire(self, at):
        with write_transaction(self.db):
            return bookings.expire_due(self.db, self.ctx(at=at))

    def test_competing_and_duplicate_requests_keep_one_active_reservation(self):
        booked = self.reserve()
        self.assertEqual((booked["status"], self.product()["status"]), ("pending", "reserved"))
        self.assertEqual(self.reserve(), booked)
        with self.assertRaises(ApiError) as repeat:
            self.reserve(key="different-key")
        self.assertEqual(repeat.exception.code, "already_reserved")
        with self.assertRaises(ApiError) as competing:
            self.reserve(actor=3, key="second-buyer")
        self.assertEqual(competing.exception.code, "product_changed")
        with self.assertRaises(ApiError) as rebound:
            self.reserve(product=2)
        self.assertEqual(rebound.exception.code, "request_key_conflict")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO reservation(product_id, buyer_id, product_title, price_cents, location, status, created_at) "
                "VALUES (1, 3, '键盘', 9900, '学生中心', 'pending', ?)",
                (self.start,),
            )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reservation").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 1)

    def test_old_page_and_old_receipt_after_cancel_then_rebook(self):
        old = self.reserve()
        canceled = self.action(2, old["id"], "cancel", "cancel-original")
        self.assertEqual(self.product()["status"], "published")
        with self.assertRaises(ApiError) as stale:
            self.reserve(key="stale-new-request")
        self.assertEqual(stale.exception.code, "product_changed")
        new = self.reserve(version=self.product()["version"], key="new-reservation")
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(self.reserve(), old)
        self.assertEqual(self.action(2, old["id"], "cancel", "cancel-original"), canceled)
        with self.assertRaises(ApiError):
            self.action(2, old["id"], "cancel", "another-old-cancel")
        with self.assertRaises(ApiError) as unauthorised:
            self.action(3, old["id"], "cancel", "cancel-original")
        self.assertEqual(unauthorised.exception.status, 403)
        rows = self.db.execute("SELECT status FROM reservation ORDER BY id").fetchall()
        self.assertEqual([row["status"] for row in rows], ["canceled", "pending"])
        self.assertEqual(self.product()["status"], "reserved")

    def test_cancel_confirm_race_order_and_seller_reason(self):
        first = self.reserve()
        self.action(2, first["id"], "cancel", "first-cancel")
        with self.assertRaises(ApiError) as already_canceled:
            self.action(1, first["id"], "confirm", "late-confirm")
        self.assertEqual(already_canceled.exception.code, "buyer_canceled")

        second = self.reserve(version=self.product()["version"], key="book-again")
        self.action(1, second["id"], "confirm", "confirm-second")
        with self.assertRaises(ApiError) as confirmed:
            self.action(2, second["id"], "cancel", "late-buyer-cancel")
        self.assertEqual(confirmed.exception.code, "seller_confirmed")
        with self.assertRaises(ApiError) as missing_reason:
            self.action(1, second["id"], "cancel", "seller-cancel", "  ")
        self.assertEqual(missing_reason.exception.status, 400)
        self.assertEqual(self.product()["status"], "reserved")
        result = self.action(1, second["id"], "cancel", "seller-cancel", "无法在约定时间交接")
        self.assertEqual((result["status"], result["reason"]), ("canceled", "无法在约定时间交接"))
        self.assertEqual(self.product()["status"], "published")

    def test_reject_notifies_buyer_and_preserves_historical_listing(self):
        initial = self.reserve()
        rejected = self.action(1, initial["id"], "reject", "reject-first")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.action(1, initial["id"], "reject", "reject-first"), rejected)
        self.db.execute("UPDATE product SET title = '新标题', price_cents = 12000 WHERE id = 1")
        history = bookings.list_reservations(self.db, self.ctx(2), {"status": "rejected"})["items"]
        self.assertEqual(
            [(r["product_title"], r["price_cents"]) for r in history], [("键盘", 9900)]
        )
        self.assertEqual(bookings.list_reservations(self.db, self.ctx(3), {})["total"], 0)
        self.assertEqual(
            bookings.list_reservations(self.db, self.ctx(4), {"scope": "admin"})["total"], 1
        )
        recipients = self.db.execute("SELECT recipient_id FROM notification").fetchall()
        self.assertEqual([row[0] for row in recipients], [2])

    def test_timeout_boundaries_do_not_repeat_transitions_or_notices(self):
        pending = self.reserve()
        second = self.reserve(product=2, key="second-product")
        confirmed_at = self.start + 20
        self.action(1, second["id"], "confirm", "confirm-2", at=confirmed_at)
        self.assertEqual(self.expire(self.start + bookings.PENDING_TTL - 1), 0)
        self.assertEqual(self.expire(self.start + bookings.PENDING_TTL), 1)
        self.assertEqual(self.expire(self.start + bookings.PENDING_TTL + 1), 0)
        self.assertEqual(self.expire(confirmed_at + bookings.CONFIRMED_TTL - 1), 0)
        self.assertEqual(self.expire(confirmed_at + bookings.CONFIRMED_TTL), 1)
        self.assertEqual(self.expire(confirmed_at + bookings.CONFIRMED_TTL + 1), 0)
        rows = self.db.execute(
            "SELECT id, status, ended_by FROM reservation ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [(pending["id"], "canceled", None), (second["id"], "completed", None)],
        )
        self.assertEqual(
            (self.product()["status"], self.product(2)["status"]), ("published", "sold")
        )
        self.assertEqual(self.product(2)["sold_at"], confirmed_at + bookings.CONFIRMED_TTL)
        recipients = self.db.execute(
            "SELECT recipient_id FROM notification ORDER BY recipient_id"
        ).fetchall()
        self.assertEqual([row[0] for row in recipients], [1, 2])

    def test_complete_and_violation_paths_do_not_overwrite_each_other(self):
        first = self.reserve()
        self.action(1, first["id"], "confirm", "confirm-1")
        complete = self.action(2, first["id"], "complete", "complete-1", at=self.start + 15)
        self.assertEqual((complete["status"], complete["completed_by"]), ("completed", 2))
        with self.assertRaises(ApiError):
            self.action(1, first["id"], "complete", "repeat-by-seller")
        active = self.reserve(product=2, key="book-2")
        with write_transaction(self.db):
            violate_product(self.db, self.ctx(4), 1, {"reason": "违规描述"})
            violate_product(self.db, self.ctx(4), 2, {"reason": "违规描述"})
        with self.assertRaises(ApiError):
            self.action(1, active["id"], "confirm", "late-confirm")
        self.assertEqual(
            (self.product()["status"], self.product(2)["status"]), ("sold", "violation")
        )
        self.assertEqual(
            (self.product()["hidden"], self.product()["sold_at"]), (1, self.start + 15)
        )
        notes = self.db.execute(
            "SELECT recipient_id, message FROM notification WHERE event_key = ? ORDER BY recipient_id",
            (f"reservation:{active['id']}:violation_cancel",),
        ).fetchall()
        self.assertEqual([row[0] for row in notes], [1, 2])
        self.assertTrue(all("违规描述" in row[1] for row in notes))
        statuses = self.db.execute("SELECT status FROM reservation ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in statuses], ["completed", "canceled"])

    def test_failed_receipt_write_rolls_back_product_booking_and_audit(self):
        self.db.execute(
            "CREATE TRIGGER reject_receipt BEFORE INSERT ON request_result "
            "BEGIN SELECT RAISE(ABORT, 'injected receipt write failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.reserve()
        self.assertEqual((self.product()["status"], self.product()["version"]), ("published", 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reservation").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER reject_receipt")
        self.assertEqual(self.reserve()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
