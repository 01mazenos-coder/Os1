"""Dynamic QR card activation platform.

Every printed card carries one QR code that points to  <BASE_URL>/c/<card_id>.

  * first scan of an unlinked card  -> activation page (shop owner pastes the destination link)
  * every later scan                -> instant redirect to the saved destination
  * /admin                          -> manage cards, change destinations, pause, export, QR images
"""
import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)

try:  # optional: server-side QR images in the admin panel
    import segno
except ImportError:  # pragma: no cover
    segno = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # no 0/o/1/l/i: easy to read off a card
ID_LENGTH = 8
ID_RE = re.compile(r"^[a-z0-9]{4,32}$")
PAGE_SIZE = 50
MAX_BATCH = 500
STATUSES = ("inactive", "active", "disabled")
STATUS_LABELS = {"inactive": "Etkinleştirilmedi", "active": "Aktif", "disabled": "Duraklatıldı"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id         TEXT PRIMARY KEY,
    label           TEXT NOT NULL DEFAULT '',
    destination_url TEXT,
    status          TEXT NOT NULL DEFAULT 'inactive'
                    CHECK (status IN ('inactive', 'active', 'disabled')),
    scan_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    activated_at    TEXT,
    updated_at      TEXT NOT NULL,
    last_scan_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards (status);
"""


def persisted_secret(env_name, filename, factory):
    """Read a secret from the environment, else from a 0600 file (created on first run)."""
    value = os.environ.get(env_name)
    if value:
        return value, False
    path = os.path.join(BASE_DIR, filename)
    created = False
    try:
        if not os.path.exists(path):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(factory())
                created = True
            except FileExistsError:  # another worker is creating it right now
                pass
        for _ in range(20):  # wait for that worker to finish writing
            with open(path) as f:
                content = f.read().strip()
            if content:
                return content, created
            time.sleep(0.1)
        raise OSError("dosya boş")
    except OSError as exc:
        raise RuntimeError(f"{env_name} ortam değişkenini ayarlayın ({exc}).") from exc


app = Flask(__name__)
secret_key, _ = persisted_secret("SECRET_KEY", ".secret_key", lambda: secrets.token_hex(32))
admin_password, _admin_pw_created = persisted_secret(
    "ADMIN_PASSWORD", ".admin_password", lambda: secrets.token_urlsafe(12))
app.config.update(
    SECRET_KEY=secret_key,
    ADMIN_PASSWORD=admin_password,
    DATABASE=os.environ.get("DATABASE", os.path.join(BASE_DIR, "cards.db")),
    BASE_URL=os.environ.get("BASE_URL", "").rstrip("/"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BASE_URL", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=12 * 3600,
)
if os.environ.get("TRUST_PROXY") == "1":  # running behind nginx / a PaaS proxy
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
if _admin_pw_created:
    print(f"[qr-platform] Generated admin password: {admin_password}  "
          f"(stored in {os.path.join(BASE_DIR, '.admin_password')})")

try:
    from zoneinfo import ZoneInfo
    DISPLAY_TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Europe/Istanbul"))
except Exception:  # unknown zone or no tzdata
    DISPLAY_TZ = timezone.utc


# --------------------------------------------------------------------------- database
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    db.close()


init_db()


def new_card_id():
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def create_cards(count, label_prefix=""):
    db = get_db()
    created = []
    for i in range(1, count + 1):
        label = f"{label_prefix} {i}".strip() if label_prefix else ""
        while True:
            card_id = new_card_id()
            ts = now_iso()
            try:
                db.execute(
                    "INSERT INTO cards (card_id, label, created_at, updated_at) VALUES (?,?,?,?)",
                    (card_id, label, ts, ts))
                break
            except sqlite3.IntegrityError:  # id collision: draw again
                continue
        created.append(card_id)
    db.commit()
    return created


def get_card(card_id):
    return get_db().execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()


# --------------------------------------------------------------------------- helpers
def card_link(card_id):
    base = app.config["BASE_URL"] or request.host_url.rstrip("/")
    return f"{base}/c/{card_id}"


def normalize_destination(raw):
    """Return a safe http(s) URL, or (None, reason)."""
    raw = (raw or "").strip()
    if not raw:
        return None, "Lütfen bağlantıyı girin."
    if len(raw) > 2048 or any(c.isspace() for c in raw):
        return None, "Bağlantı geçersiz (boşluk içeriyor veya çok uzun)."
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "." not in parsed.hostname:
        return None, "Lütfen http:// veya https:// ile başlayan geçerli bir bağlantı girin."
    own_host = (request.host or "").split(":")[0].lower()
    if parsed.hostname.lower() == own_host:
        return None, "Bağlantı bu platformun kendisine işaret edemez."
    return raw, None


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def fmt_dt(value):
    if not value:
        return "—"
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def int_arg(name, default, minimum=1):
    try:
        return max(minimum, int(request.args.get(name, default)))
    except (TypeError, ValueError):
        return default


app.jinja_env.globals.update(csrf_token=csrf_token, card_link=card_link,
                             STATUS_LABELS=STATUS_LABELS, segno_available=segno is not None)
app.jinja_env.filters["dt"] = fmt_dt


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Sayfanın süresi doldu. Lütfen sayfayı yenileyip tekrar deneyin.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def message_page(title, text, status=200, link=None, link_text=None):
    return render_template("message.html", title=title, text=text, link=link,
                           link_text=link_text), status


@app.errorhandler(400)
def bad_request(err):
    return message_page("Geçersiz istek", getattr(err, "description", "") or "Bir hata oluştu.", 400)


@app.errorhandler(404)
def not_found(_err):
    return message_page("Sayfa bulunamadı", "Bu bağlantı hatalı ya da kart sistemimizde kayıtlı değil.", 404)


# --------------------------------------------------------------------------- public: scan + activation
@app.route("/healthz")
def healthz():
    get_db().execute("SELECT 1").fetchone()
    return "ok", 200, {"Cache-Control": "no-store", "Content-Type": "text/plain"}


@app.route("/")
def index():
    return message_page("Akıllı Kart Platformu", "Başlamak için kartınızı telefon kameranızla okutun.")


@app.route("/c/<card_id>")
def scan(card_id):
    card_id = card_id.lower()
    card = get_card(card_id) if ID_RE.match(card_id) else None
    if card is None:
        abort(404)

    if card["status"] == "disabled":
        return message_page("Kart duraklatıldı", "Bu kart geçici olarak duraklatıldı. Lütfen işletme sahibiyle iletişime geçin.", 403)

    if card["status"] == "inactive":
        return render_template("activate.html", card=card, error=None, value="")

    # active
    if session.pop("just_activated", None) == card_id:  # first view right after activation
        return render_template("message.html", title="Kart etkinleştirildi ✓",
                               text="Bağlantı kaydedildi. Bundan sonra bu kartın her okutulmasında bağlantı doğrudan açılacak.",
                               link=card["destination_url"], link_text="Bağlantıyı dene")
    db = get_db()
    db.execute("UPDATE cards SET scan_count = scan_count + 1, last_scan_at = ? WHERE card_id = ?",
               (now_iso(), card_id))
    db.commit()
    resp = redirect(card["destination_url"], code=302)  # 302: never cached, so edits apply instantly
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/c/<card_id>/activate")
def activate(card_id):
    card_id = card_id.lower()
    card = get_card(card_id) if ID_RE.match(card_id) else None
    if card is None:
        abort(404)
    if card["status"] != "inactive":
        return message_page("Kart zaten etkin",
                            "Bu kart zaten bir bağlantıya bağlı. Değiştirmek için hizmet sağlayıcınızla iletişime geçin.", 409)

    url, error = normalize_destination(request.form.get("destination"))
    if error:
        return render_template("activate.html", card=card, error=error,
                               value=request.form.get("destination", "")[:300]), 400

    db = get_db()
    ts = now_iso()
    # atomic: only the first request to arrive can activate the card
    cur = db.execute(
        "UPDATE cards SET destination_url = ?, status = 'active', activated_at = ?, updated_at = ? "
        "WHERE card_id = ? AND status = 'inactive'", (url, ts, ts, card_id))
    db.commit()
    if cur.rowcount != 1:
        return message_page("Kart zaten etkin", "Bu kartı sizden önce başka biri etkinleştirdi.", 409)
    session["just_activated"] = card_id
    return redirect(url_for("scan", card_id=card_id))


# --------------------------------------------------------------------------- admin
_failed_logins = {}


def login_blocked(ip):
    cutoff = time.time() - 600
    attempts = [t for t in _failed_logins.get(ip, []) if t > cutoff]
    _failed_logins[ip] = attempts
    return len(attempts) >= 5


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if login_blocked(ip):
            error = "Çok fazla deneme. 10 dakika bekleyip tekrar deneyin."
        elif hmac.compare_digest(request.form.get("password", ""), app.config["ADMIN_PASSWORD"]):
            session.clear()
            session["admin"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                nxt = url_for("admin_dashboard")
            return redirect(nxt)
        else:
            _failed_logins.setdefault(ip, []).append(time.time())
            error = "Şifre hatalı."
    return render_template("admin_login.html", error=error), (401 if error else 200)


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def _filters():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    where, params = [], []
    if status in STATUSES:
        where.append("status = ?")
        params.append(status)
    else:
        status = ""
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(card_id LIKE ? ESCAPE '\\' OR label LIKE ? ESCAPE '\\' "
                     "OR destination_url LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return status, q, clause, params


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    status, q, clause, params = _filters()
    total = db.execute(f"SELECT COUNT(*) FROM cards{clause}", params).fetchone()[0]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(int_arg("page", 1), pages)
    cards = db.execute(
        f"SELECT * FROM cards{clause} ORDER BY created_at DESC, rowid ASC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    counts = {s: 0 for s in STATUSES}
    for row in db.execute("SELECT status, COUNT(*) AS n FROM cards GROUP BY status"):
        counts[row["status"]] = row["n"]
    scans = db.execute("SELECT COALESCE(SUM(scan_count), 0) FROM cards").fetchone()[0]
    return render_template("admin_dashboard.html", cards=cards, counts=counts, scans=scans,
                           all_total=sum(counts.values()), status=status, q=q, page=page,
                           pages=pages, matched=total, max_batch=MAX_BATCH)


@app.post("/admin/cards/create")
@admin_required
def admin_create():
    try:
        count = int(request.form.get("count", "0"))
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_BATCH:
        flash(f"1 ile {MAX_BATCH} arasında bir sayı girin.", "error")
        return redirect(url_for("admin_dashboard"))
    create_cards(count, request.form.get("label_prefix", "").strip()[:60])
    flash(f"{count} yeni kart oluşturuldu (etkinleştirilmedi).", "ok")
    return redirect(url_for("admin_dashboard", status="inactive"))


@app.route("/admin/cards/<card_id>", methods=["GET", "POST"])
@admin_required
def admin_card(card_id):
    card = get_card(card_id.lower()) if ID_RE.match(card_id.lower()) else None
    if card is None:
        abort(404)
    error = None
    if request.method == "POST":
        label = request.form.get("label", "").strip()[:120]
        raw_url = request.form.get("destination", "").strip()
        paused = request.form.get("paused") == "1"
        url = None
        if raw_url:
            url, error = normalize_destination(raw_url)
        if not error:
            status = "disabled" if paused else ("active" if url else "inactive")
            ts = now_iso()
            db = get_db()
            db.execute(
                "UPDATE cards SET label = ?, destination_url = ?, status = ?, updated_at = ?, "
                "activated_at = CASE WHEN ? = 'active' AND activated_at IS NULL THEN ? ELSE activated_at END "
                "WHERE card_id = ?", (label, url, status, ts, status, ts, card["card_id"]))
            db.commit()
            flash("Değişiklikler kaydedildi.", "ok")
            return redirect(url_for("admin_card", card_id=card["card_id"]))
    return render_template("admin_card.html", card=card, error=error), (400 if error else 200)


@app.post("/admin/cards/<card_id>/delete")
@admin_required
def admin_delete(card_id):
    db = get_db()
    db.execute("DELETE FROM cards WHERE card_id = ?", (card_id.lower(),))
    db.commit()
    flash("Kart silindi. Bu kodu taşıyan basılı kart artık kayıtsız görünecek.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/cards/<card_id>/qr.svg")
@admin_required
def admin_qr(card_id):
    card = get_card(card_id.lower()) if ID_RE.match(card_id.lower()) else None
    if card is None or segno is None:
        abort(404)
    buf = io.BytesIO()
    segno.make(card_link(card["card_id"]), error="m").save(
        buf, kind="svg", scale=10, border=2, xmldecl=False)
    resp = Response(buf.getvalue(), mimetype="image/svg+xml")
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="card-{card["card_id"]}.svg"'
    return resp


@app.route("/admin/export.csv")
@admin_required
def admin_export():
    """Printer-friendly list: card id, the exact link to encode in the QR, label, status."""
    _status, _q, clause, params = _filters()
    rows = get_db().execute(f"SELECT * FROM cards{clause} ORDER BY created_at, rowid", params)
    out = io.StringIO()
    out.write("﻿")  # BOM so Excel reads Arabic correctly
    writer = csv.writer(out)
    writer.writerow(["card_id", "qr_link", "label", "status", "destination", "scans"])
    for r in rows:
        writer.writerow([r["card_id"], card_link(r["card_id"]), r["label"], r["status"],
                         r["destination_url"] or "", r["scan_count"]])
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="cards.csv"'})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
