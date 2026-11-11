"""Integration tests exercising real HTTP requests and persistent SQLite state."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from campus_trade import accounts
from campus_trade.core import database, initialize, write_transaction
from campus_trade.server import CampusServer

DEMO_PASSWORD = "test-password-3"
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jLykAAAAASUVORK5CYII="
)


class BrowserClient:
    """Independent browser session backed by an HTTP cookie jar."""

    def __init__(self, base):
        self.base = base
        self.http = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, path, data=None):
        payload = None if data is None else json.dumps(data).encode()
        request = Request(self.base + path, data=payload)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.http.open(request, timeout=8) as reply:
                return self._decode(reply)
        except HTTPError as error:
            with error:
                return self._decode(error)

    @staticmethod
    def _decode(reply):
        content = reply.read()
        if reply.headers.get_content_type() == "application/json":
            content = json.loads(content)
        return reply.code, content

    def register(self, phone, nickname):
        _, issued = self.request("/api/auth/code", {"phone": phone})
        status, _ = self.request(
            "/api/auth/register",
            {
                "phone": phone,
                "nickname": nickname,
                "password": DEMO_PASSWORD,
                "code": issued["development_code"],
            },
        )
        if status != 200:
            raise AssertionError("Registration failed")
        return self.login(phone)

    def login(self, phone):
        status, payload = self.request(
            "/api/auth/login", {"phone": phone, "password": DEMO_PASSWORD}
        )
        if status != 200:
            raise AssertionError(payload)
        return payload["account"]


class HttpApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trade.sqlite3"
        initialize(self.path)
        with database(self.path) as db, write_transaction(db):
            accounts.bootstrap_super(db, "13800000000", "超管", DEMO_PASSWORD)
        self.server = CampusServer(self.path, 0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.guest = BrowserClient(self.base)
        self.superuser = BrowserClient(self.base)
        self.superuser.login("13800000000")

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=4)

    def create_member(self, suffix, nickname):
        user = BrowserClient(self.base)
        user.register(f"1380000000{suffix}", nickname)
        return user

    def publish(self, seller, title="键盘"):
        _, draft = seller.request(
            "/api/products",
            {
                "title": title,
                "description": "机械键盘",
                "category_id": 2,
                "price_cents": 9900,
                "location": "鼓楼校区",
                "photos": [{"mime": "image/png", "data": PNG}],
            },
        )
        product_url = f"/api/products/{draft['id']}"
        code, submitted = seller.request(product_url + "/submit", {"version": draft["version"]})
        self.assertEqual((code, submitted["status"]), (200, "pending_review"))
        _, queue = self.superuser.request("/api/admin/reviews")
        review_id = next(row["id"] for row in queue["items"] if row["product_id"] == draft["id"])
        code, product = self.superuser.request(
            f"/api/admin/reviews/{review_id}", {"decision": "approve"}
        )
        self.assertEqual((code, product["status"]), (200, "published"))
        return product_url, product

    def test_trade_and_public_visibility(self):
        seller = self.create_member(1, "卖家")
        buyer = self.create_member(2, "买家")
        path, published = self.publish(seller)
        self.assertEqual(
            self.guest.request("/api/products?" + urlencode({"category_id": 2, "q": "机械"}))[1][
                "total"
            ],
            1,
        )
        self.assertEqual(self.guest.request(published["photos"][0]["url"])[0], 200)
        reserve = {"version": published["version"], "request_key": "new-reservation"}
        status, booking = buyer.request(path + "/reserve", reserve)
        self.assertEqual((status, booking["status"]), (200, "pending"))
        self.assertEqual(buyer.request(path + "/reserve", reserve)[1]["id"], booking["id"])
        self.assertEqual(self.guest.request("/api/products")[1]["total"], 0)
        booking_url = f"/api/reservations/{booking['id']}"
        status, confirmed = seller.request(booking_url + "/confirm", {"request_key": "confirm"})
        self.assertEqual((status, confirmed["status"]), (200, "confirmed"))
        status, refused = buyer.request(booking_url + "/cancel", {"request_key": "cancel"})
        self.assertEqual(status, 409, refused)
        status, done = buyer.request(booking_url + "/complete", {"request_key": "complete"})
        self.assertEqual((status, done["status"]), (200, "completed"))
        self.assertEqual(self.guest.request(path)[1]["status"], "sold")
        self.assertEqual(seller.request("/api/stats?scope=mine")[1]["completed"], 1)
        self.assertEqual(buyer.request("/api/reservations?scope=buyer")[1]["total"], 1)
        _, logs = buyer.request("/api/logs?scope=mine&action=reservation.cancel")
        self.assertEqual(logs["total"], 1)
        self.assertFalse(logs["items"][0]["success"])
        self.assertNotIn("ip", logs["items"][0])

    def test_permissions_and_initial_password(self):
        member = self.create_member(1, "普通用户")
        self.assertEqual(member.request("/api/admin/reviews")[0], 403)
        self.assertEqual(self.guest.request("/api/products", {"title": "草稿"})[0], 401)
        code, _ = self.superuser.request(
            "/api/admin/accounts",
            {"phone": "13800000009", "nickname": "审核员", "password": DEMO_PASSWORD},
        )
        self.assertEqual(code, 200)
        administrator = BrowserClient(self.base)
        self.assertTrue(administrator.login("13800000009")["must_change"])
        self.assertEqual(administrator.request("/api/admin/reviews")[0], 403)
        code, result = administrator.request(
            "/api/auth/password",
            {"old_password": DEMO_PASSWORD, "new_password": "changed-admin-password"},
        )
        self.assertEqual(code, 200)
        self.assertFalse(result["account"]["must_change"])
        self.assertEqual(administrator.request("/api/admin/reviews")[0], 200)
        self.assertEqual(administrator.request("/api/admin/accounts")[0], 403)
        with database(self.path) as db:
            hashes = [row[0] for row in db.execute("SELECT password_hash FROM account")]
            self.assertTrue(all(DEMO_PASSWORD not in value for value in hashes))
            self.assertNotIn("changed-admin-password", str(hashes))
            for row in db.execute("SELECT detail FROM audit_log"):
                self.assertNotIn(DEMO_PASSWORD, str(row[0]))

    def test_two_buyers_racing_through_http(self):
        seller = self.create_member(1, "卖家")
        buyer = self.create_member(2, "买家甲")
        other = self.create_member(3, "买家乙")
        path, published = self.publish(seller, "抢约测试商品")
        barrier = threading.Barrier(2)

        def try_reserve(client):
            barrier.wait(timeout=2)
            return client.request(
                path + "/reserve",
                {"version": published["version"], "request_key": "race"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(try_reserve, [buyer, other]))
        self.assertEqual(sorted(code for code, _ in responses), [200, 409])
        _, rows = seller.request("/api/reservations?scope=seller")
        self.assertEqual(rows["total"], 1)
        self.assertEqual(seller.request(path)[1]["status"], "reserved")

    def test_static_entrypoints_and_malformed_input(self):
        for path in ["/", "/static/app.js", "/static/style.css"]:
            status, body = self.guest.request(path)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 0)
        self.assertEqual(self.guest.request("/static/../server.py")[0], 404)
        self.assertEqual(self.guest.request("/api/products?minimum=300&maximum=100")[0], 400)
        self.assertEqual(self.guest.request("/api/auth/code", {"phone": []})[0], 400)
