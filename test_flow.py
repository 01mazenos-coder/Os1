"""End-to-end test: create -> scan -> activate -> redirect -> admin edit/pause/reset/delete."""
import os
import re
import sqlite3
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ.update(DATABASE=os.path.join(_tmp, "t.db"), SECRET_KEY="test-secret",
                  ADMIN_PASSWORD="pw-test", BASE_URL="https://cards.example.com")

import app as appmod  # noqa: E402

appmod.app.config["PREFERRED_URL_SCHEME"] = "https"  # session cookie is Secure when BASE_URL is https

DEST = "https://g.page/r/abc123/review"


def token(client):
    """CSRF token of the client's session (render any page first if the session has none yet)."""
    with client.session_transaction() as s:
        tok = s.get("csrf")
    if not tok:
        client.get("/admin/login")
        with client.session_transaction() as s:
            tok = s["csrf"]
    return tok


class Flow(unittest.TestCase):
    def setUp(self):
        with sqlite3.connect(os.environ["DATABASE"]) as db:
            db.execute("DELETE FROM cards")  # every test starts from an empty table
        self.visitor = appmod.app.test_client()
        self.admin = appmod.app.test_client()
        self.admin.get("/admin/login")
        r = self.admin.post("/admin/login", data={"password": "pw-test", "_csrf": token(self.admin)})
        self.assertEqual(r.status_code, 302)
        r = self.admin.post("/admin/cards/create", data={"count": "1", "label_prefix": "T",
                                                          "_csrf": token(self.admin)})
        self.assertEqual(r.status_code, 302)
        with sqlite3.connect(os.environ["DATABASE"]) as db:
            self.cid = db.execute("SELECT card_id FROM cards ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]

    def row(self):
        with sqlite3.connect(os.environ["DATABASE"]) as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM cards WHERE card_id=?", (self.cid,)).fetchone()

    def activate(self, url, client=None):
        c = client or self.visitor
        c.get(f"/c/{self.cid}")
        return c.post(f"/c/{self.cid}/activate", data={"destination": url, "_csrf": token(c)})

    def test_unknown_card_404(self):
        self.assertEqual(self.visitor.get("/c/zzzzzzzz").status_code, 404)
        self.assertEqual(self.visitor.get("/c/../etc").status_code, 404)

    def test_first_scan_shows_activation_page(self):
        r = self.visitor.get(f"/c/{self.cid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="destination"', r.get_data(as_text=True))
        self.assertEqual(self.row()["status"], "inactive")

    def test_csrf_required(self):
        r = self.visitor.post(f"/c/{self.cid}/activate", data={"destination": DEST})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.row()["status"], "inactive")

    def test_rejects_bad_urls(self):
        for bad in ["javascript:alert(1)", "ftp://x.com/f", "", "not a url",
                    "http://localhost/x", "https://"]:
            with self.subTest(bad=bad):
                r = self.activate(bad)
                self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.row()["status"], "inactive")

    def test_rejects_link_to_platform_itself(self):
        self.visitor.get(f"/c/{self.cid}", base_url="https://cards.example.com")
        r = self.visitor.post(f"/c/{self.cid}/activate", base_url="https://cards.example.com",
                              data={"destination": "https://cards.example.com/c/abcd2345", "_csrf": token(self.visitor)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.row()["status"], "inactive")

    def test_activate_then_permanent_redirect(self):
        r = self.activate(DEST)
        self.assertEqual(r.status_code, 302)
        # first view after activation is the success page (not counted as a scan)
        ok = self.visitor.get(f"/c/{self.cid}")
        self.assertEqual(ok.status_code, 200)
        self.assertIn(DEST, ok.get_data(as_text=True))
        self.assertEqual(self.row()["scan_count"], 0)
        # every later scan, from any device, redirects
        for i in range(1, 4):
            other = appmod.app.test_client()
            r = other.get(f"/c/{self.cid}")
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers["Location"], DEST)
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertEqual(self.row()["scan_count"], i)
        self.assertEqual(self.row()["status"], "active")

    def test_cannot_reactivate_or_hijack(self):
        self.activate(DEST)
        r = self.activate("https://evil.example.org")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.row()["destination_url"], DEST)

    def test_scheme_less_url_gets_https(self):
        self.activate("instagram.com/myshop")
        self.assertEqual(self.row()["destination_url"], "https://instagram.com/myshop")

    def test_admin_required(self):
        for path in ["/admin", f"/admin/cards/{self.cid}", "/admin/export.csv"]:
            r = self.visitor.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/admin/login", r.headers["Location"])
        bad = appmod.app.test_client()
        bad.get("/admin/login")
        r = bad.post("/admin/login", data={"password": "nope", "_csrf": token(bad)})
        self.assertEqual(r.status_code, 401)

    def test_admin_edit_pause_reset(self):
        self.activate(DEST)
        self.visitor.get(f"/c/{self.cid}")  # consume the one-time "activated" success page
        a = self.admin
        a.get(f"/admin/cards/{self.cid}")
        # change destination -> new scans go to the new link
        new = "https://www.instagram.com/shop"
        r = a.post(f"/admin/cards/{self.cid}", data={"label": "Shop", "destination": new, "_csrf": token(a)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.visitor.get(f"/c/{self.cid}").headers["Location"], new)
        # pause
        a.post(f"/admin/cards/{self.cid}", data={"label": "Shop", "destination": new, "paused": "1", "_csrf": token(a)})
        self.assertEqual(self.visitor.get(f"/c/{self.cid}").status_code, 403)
        # un-pause
        a.post(f"/admin/cards/{self.cid}", data={"label": "Shop", "destination": new, "_csrf": token(a)})
        self.assertEqual(self.visitor.get(f"/c/{self.cid}").status_code, 302)
        # invalid url in admin is rejected, nothing changes
        r = a.post(f"/admin/cards/{self.cid}", data={"label": "x", "destination": "javascript:1", "_csrf": token(a)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.row()["destination_url"], new)
        # clear -> back to inactive, can be activated again
        a.post(f"/admin/cards/{self.cid}", data={"label": "Shop", "destination": "", "_csrf": token(a)})
        self.assertEqual(self.row()["status"], "inactive")
        self.assertIn('name="destination"', self.visitor.get(f"/c/{self.cid}").get_data(as_text=True))

    def test_admin_can_link_inactive_card_directly(self):
        a = self.admin
        a.get(f"/admin/cards/{self.cid}")
        a.post(f"/admin/cards/{self.cid}", data={"label": "", "destination": DEST, "_csrf": token(a)})
        self.assertEqual(self.row()["status"], "active")
        self.assertIsNotNone(self.row()["activated_at"])

    def test_dashboard_search_and_export(self):
        self.activate(DEST)
        a = self.admin
        html = a.get("/admin").get_data(as_text=True)
        self.assertIn(self.cid, html)
        self.assertIn(self.cid, a.get("/admin?q=g.page&status=active").get_data(as_text=True))
        self.assertNotIn(self.cid, a.get("/admin?status=inactive").get_data(as_text=True))
        self.assertNotIn(self.cid, a.get("/admin?q=100%25_x").get_data(as_text=True))
        csv_text = a.get("/admin/export.csv").get_data(as_text=True)
        self.assertIn(f"https://cards.example.com/c/{self.cid}", csv_text)
        self.assertTrue(a.get("/admin?page=abc").status_code == 200)

    def test_bulk_create_and_unique_ids(self):
        a = self.admin
        a.get("/admin")
        a.post("/admin/cards/create", data={"count": "200", "label_prefix": "", "_csrf": token(a)})
        with sqlite3.connect(os.environ["DATABASE"]) as db:
            ids = [r[0] for r in db.execute("SELECT card_id FROM cards")]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.fullmatch(r"[2-9a-hjkmnp-z]{8}", i) for i in ids))
        r = a.post("/admin/cards/create", data={"count": "100000", "_csrf": token(a)})
        self.assertEqual(r.status_code, 302)  # rejected with a flash, nothing created

    def test_delete(self):
        self.activate(DEST)
        a = self.admin
        a.get(f"/admin/cards/{self.cid}")
        a.post(f"/admin/cards/{self.cid}/delete", data={"_csrf": token(a)})
        self.assertEqual(self.visitor.get(f"/c/{self.cid}").status_code, 404)

    def test_open_redirect_on_login_blocked(self):
        c = appmod.app.test_client()
        c.get("/admin/login")
        r = c.post("/admin/login?next=//evil.com", data={"password": "pw-test", "_csrf": token(c)})
        self.assertEqual(r.headers["Location"], "/admin")

    def test_interface_is_turkish(self):
        page = self.visitor.get(f"/c/{self.cid}").get_data(as_text=True)
        self.assertIn('lang="tr"', page)
        self.assertIn("Kartı Etkinleştir", page)
        self.activate(DEST)
        again = self.visitor.post(f"/c/{self.cid}/activate",
                                  data={"destination": "https://x.com", "_csrf": token(self.visitor)})
        self.assertEqual(again.status_code, 409)
        self.assertIn("Kart zaten etkin", again.get_data(as_text=True))
        self.assertIn("Yönetim Paneli", self.visitor.get("/admin/login").get_data(as_text=True))
        self.assertIn("Sayfa bulunamadı", self.visitor.get("/c/zzzzzzzz").get_data(as_text=True))

    def test_healthz(self):
        r = self.visitor.get("/healthz")
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
