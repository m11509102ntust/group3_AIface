import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY") or None
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("WEBSITE_SITE_NAME"))

MAX_IMAGE_BYTES = 5 * 1024 * 1024
PIN_LOGIN_MAX_ATTEMPTS = 5
PIN_LOGIN_WINDOW = 15 * 60
_pin_login_attempts = defaultdict(deque)
_pin_login_lock = threading.Lock()
PIN_ITERATIONS = 310_000
PIN_PATTERN = re.compile(r"^\d{6,12}$")
DUMMY_PIN_SALT = "00" * 16
DUMMY_PIN_HASH = hashlib.pbkdf2_hmac(
    "sha256",
    b"invalid-pin",
    bytes.fromhex(DUMMY_PIN_SALT),
    PIN_ITERATIONS,
).hex()


def _checkin_settings_ready():
    return bool(
        os.getenv("CHECKIN_ADMIN_PASSWORD", "").strip()
        and app.secret_key
    )


def _database_path():
    configured_path = os.getenv("CHECKIN_DB_PATH", "").strip()
    if configured_path:
        return Path(configured_path)

    if os.getenv("WEBSITE_SITE_NAME"):
        return Path(os.getenv("HOME", r"D:\home")) / "data" / "checkins.sqlite3"
    return Path(app.instance_path) / "checkins.sqlite3"


def _connect_db():
    database_path = _database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            pin_salt TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    people_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(people)")
    }
    if "pin_salt" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN pin_salt TEXT")
    if "pin_hash" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN pin_hash TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS mytable (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES people(id),
            "姓名" TEXT NOT NULL,
            "打卡時間" TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS face_samples (
            person_id INTEGER PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
            image BLOB NOT NULL,
            content_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consented_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS enrollment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            pin_salt TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            image BLOB NOT NULL,
            content_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consented_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        'CREATE INDEX IF NOT EXISTS idx_mytable_checkin_time ON mytable("打卡時間")'
    )
    connection.commit()
    return connection


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_is_valid():
    token = session.get("csrf_token", "")
    supplied_token = request.headers.get("X-CSRF-Token") or request.form.get(
        "csrf_token", ""
    )
    return bool(token and supplied_token and hmac.compare_digest(token, supplied_token))


def _normalize_name(name):
    return " ".join(name.split())


def _decode_face_image(image_base64):
    if not isinstance(image_base64, str) or not image_base64:
        return None, None, ("請提供有效的人臉照片。", 400)
    if len(image_base64) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        return None, None, ("照片大小不可超過 5 MB。", 413)
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError):
        return None, None, ("照片必須是有效的 Base64 編碼。", 400)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return None, None, ("照片大小不可超過 5 MB。", 413)
    if image_bytes.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    elif image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    else:
        return None, None, ("照片格式只支援 JPEG 或 PNG。", 400)
    return image_bytes, content_type, None


def _hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    pin_hash = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        bytes.fromhex(salt),
        PIN_ITERATIONS,
    ).hex()
    return salt, pin_hash


def _allow_pin_login(name):
    now = time.monotonic()
    key = name.casefold()
    with _pin_login_lock:
        for attempt_name, timestamps in list(_pin_login_attempts.items()):
            while timestamps and now - timestamps[0] >= PIN_LOGIN_WINDOW:
                timestamps.popleft()
            if not timestamps:
                del _pin_login_attempts[attempt_name]

        attempts = _pin_login_attempts[key]
        if len(attempts) >= PIN_LOGIN_MAX_ATTEMPTS:
            return False
        attempts.append(now)
        return True


def _clear_pin_login_attempts(name):
    with _pin_login_lock:
        _pin_login_attempts.pop(name.casefold(), None)


def _admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not _checkin_settings_ready():
            return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
        if not session.get("checkin_admin_authenticated"):
            return jsonify(error="請先登入管理員帳號。"), 401
        return view(*args, **kwargs)

    return wrapped_view


def _employee_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not _checkin_settings_ready():
            return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
        person_id = session.get("checkin_person_id")
        session_pin_hash = session.get("checkin_pin_hash", "")
        if not isinstance(person_id, int) or not session_pin_hash:
            return jsonify(error="請先使用個人姓名與 PIN 登入。"), 401
        try:
            with closing(_connect_db()) as connection:
                person = connection.execute(
                    "SELECT pin_hash FROM people WHERE id = ?", (person_id,)
                ).fetchone()
        except sqlite3.Error:
            app.logger.exception("Could not validate employee session")
            return jsonify(error="打卡資料庫暫時無法使用。"), 503
        if person is None or not hmac.compare_digest(
            person["pin_hash"] or "", session_pin_hash
        ):
            session.pop("checkin_person_id", None)
            session.pop("checkin_pin_hash", None)
            return jsonify(error="登入狀態已失效，請重新使用個人 PIN 登入。"), 401
        return view(*args, **kwargs)

    return wrapped_view


@app.get("/")
def index():
    checkin_password_configured = bool(
        os.getenv("CHECKIN_ADMIN_PASSWORD", "").strip()
    )
    flask_secret_configured = bool(app.secret_key)
    checkin_configured = checkin_password_configured and flask_secret_configured
    admin_authenticated = bool(
        checkin_configured and session.get("checkin_admin_authenticated")
    )
    employee_id = session.get("checkin_person_id") if checkin_configured else None
    employee_authenticated = isinstance(employee_id, int)
    people = []
    enrollment_requests = []
    checkins = []
    employee_name = None
    database_error = None

    dashboard_metrics = None
    database_ready = False
    try:
        with closing(_connect_db()) as connection:
            database_ready = True
            if admin_authenticated:
                people = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id, name, pin_hash IS NOT NULL AS has_pin, "
                        "EXISTS (SELECT 1 FROM face_samples "
                        "WHERE face_samples.person_id = people.id) AS has_face_sample "
                        "FROM people ORDER BY name COLLATE NOCASE"
                    )
                ]
                enrollment_requests = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id, name, created_at FROM enrollment_requests "
                        "ORDER BY id"
                    )
                ]
            if employee_authenticated:
                person = connection.execute(
                    "SELECT name, pin_hash FROM people WHERE id = ?", (employee_id,)
                ).fetchone()
                if (
                    person is None
                    or not session.get("checkin_pin_hash")
                    or not hmac.compare_digest(
                        person["pin_hash"] or "",
                        session["checkin_pin_hash"],
                    )
                ):
                    session.pop("checkin_person_id", None)
                    session.pop("checkin_pin_hash", None)
                    employee_authenticated = False
                else:
                    employee_name = person["name"]

            if admin_authenticated or employee_authenticated:
                today = datetime.now(timezone.utc).date().isoformat() + "%"
                record_filter = ""
                query_params = [today]
                if employee_authenticated and not admin_authenticated:
                    record_filter = " AND person_id = ?"
                    query_params.append(employee_id)

                today_summary = connection.execute(
                    'SELECT COUNT(*) AS checkin_count, '
                    "COUNT(DISTINCT person_id) AS attendee_count "
                    'FROM mytable WHERE "打卡時間" LIKE ?' + record_filter,
                    query_params,
                ).fetchone()
                dashboard_metrics = {
                    "today_checkins": today_summary["checkin_count"],
                    "today_attendees": today_summary["attendee_count"],
                    "people_count": (
                        connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
                        if admin_authenticated
                        else None
                    ),
                }

                records_query = (
                    'SELECT "姓名" AS name, "打卡時間" AS checkin_time '
                    "FROM mytable"
                )
                records_params = []
                if employee_authenticated and not admin_authenticated:
                    records_query += " WHERE person_id = ?"
                    records_params.append(employee_id)
                records_query += " ORDER BY id DESC LIMIT 50"
                checkins = [
                    dict(row)
                    for row in connection.execute(records_query, records_params)
                ]
    except sqlite3.Error:
        app.logger.exception("Could not load SQLite dashboard and check-in data")
        database_ready = False
        database_error = "無法讀取打卡資料庫，請稍後再試。"

    return render_template(
        "index.html",
        checkin_configured=checkin_configured,
        checkin_password_configured=checkin_password_configured,
        flask_secret_configured=flask_secret_configured,
        database_ready=database_ready,
        admin_authenticated=admin_authenticated,
        employee_authenticated=employee_authenticated,
        employee_name=employee_name,
        dashboard_metrics=dashboard_metrics,
        csrf_token=_csrf_token() if checkin_configured else "",
        people=people,
        enrollment_requests=enrollment_requests,
        checkins=checkins,
        database_error=database_error,
    )


@app.post("/login")
def login():
    if not _checkin_settings_ready():
        return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
    if not _csrf_is_valid():
        return "CSRF 驗證失敗，請重新載入頁面。", 400

    supplied_password = request.form.get("password", "")
    expected_password = os.environ["CHECKIN_ADMIN_PASSWORD"]
    if not _allow_pin_login("admin:login"):
        return redirect(url_for("index", admin_login_error=2))
    if not hmac.compare_digest(supplied_password, expected_password):
        return redirect(url_for("index", admin_login_error=1))

    _clear_pin_login_attempts("admin:login")
    session.clear()
    session["checkin_admin_authenticated"] = True
    _csrf_token()
    return redirect(url_for("index"))


@app.post("/person-login")
def person_login():
    if not _checkin_settings_ready():
        return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
    if not _csrf_is_valid():
        return "CSRF 驗證失敗，請重新載入頁面。", 400

    name = _normalize_name(request.form.get("name", ""))
    pin = request.form.get("pin", "")
    if not name or len(name) > 80 or not PIN_PATTERN.fullmatch(pin):
        return redirect(url_for("index", person_login_error=1))
    if not _allow_pin_login(name):
        return redirect(url_for("index", person_login_error=2))

    try:
        with closing(_connect_db()) as connection:
            person = connection.execute(
                "SELECT id, name, pin_salt, pin_hash FROM people "
                "WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
    except sqlite3.Error:
        app.logger.exception("Could not load person for PIN login")
        return "登入服務暫時無法使用，請稍後再試。", 500

    has_pin = bool(person is not None and person["pin_salt"] and person["pin_hash"])
    salt = person["pin_salt"] if has_pin else DUMMY_PIN_SALT
    stored_hash = person["pin_hash"] if has_pin else DUMMY_PIN_HASH
    _, candidate_hash = _hash_pin(pin, salt)
    valid_pin = has_pin and hmac.compare_digest(candidate_hash, stored_hash)

    if not valid_pin:
        return redirect(url_for("index", person_login_error=1))

    _clear_pin_login_attempts(name)
    session.clear()
    session["checkin_person_id"] = person["id"]
    session["checkin_pin_hash"] = person["pin_hash"]
    _csrf_token()
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    if not _checkin_settings_ready():
        return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
    if not (
        session.get("checkin_admin_authenticated")
        or isinstance(session.get("checkin_person_id"), int)
    ):
        return jsonify(error="請先登入。"), 401
    if not _csrf_is_valid():
        return "CSRF 驗證失敗，請重新載入頁面。", 400
    session.clear()
    return redirect(url_for("index"))


@app.post("/api/people")
@_admin_required
def register_person():
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    name = payload.get("name")
    pin = payload.get("pin")
    if not isinstance(name, str):
        return jsonify(error="請輸入有效的人名。"), 400
    name = _normalize_name(name)
    if not name or len(name) > 80:
        return jsonify(error="人名不可空白且不得超過 80 個字元。"), 400
    if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
        return jsonify(error="PIN 必須為 6 至 12 位數字。"), 400

    image_base64 = payload.get("face_image_base64")
    image_bytes = None
    content_type = None
    if "face_image_base64" in payload:
        image_bytes, content_type, image_error = _decode_face_image(image_base64)
        if image_error:
            return jsonify(error=image_error[0]), image_error[1]
        if payload.get("face_image_consent") is not True:
            return jsonify(error="儲存人臉照片前必須確認已取得本人明確同意。"), 400

    confirm_pin_reset = payload.get("confirm_pin_reset") is True
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    salt, pin_hash = _hash_pin(pin)

    try:
        with closing(_connect_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            person = connection.execute(
                "SELECT id, name FROM people WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()

            if person is None:
                return jsonify(
                    error="新進人員必須先提交建檔申請，並由管理員核准。"
                ), 409
            if not confirm_pin_reset:
                return jsonify(
                    needs_reset_confirmation=True,
                    name=person["name"],
                    message=f"「{person['name']}」已存在，是否重設其 PIN？",
                ), 409

            name = person["name"]
            person_id = person["id"]
            connection.execute(
                "UPDATE people SET pin_salt = ?, pin_hash = ? WHERE id = ?",
                (salt, pin_hash, person["id"]),
            )
            result_status = 200
            if image_bytes is not None:
                connection.execute(
                    "INSERT INTO face_samples "
                    "(person_id, image, content_type, created_at, consented_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(person_id) DO UPDATE SET "
                    "image = excluded.image, content_type = excluded.content_type, "
                    "created_at = excluded.created_at, "
                    "consented_at = excluded.consented_at",
                    (
                        person_id,
                        image_bytes,
                        content_type,
                        created_at,
                        created_at,
                    ),
                )
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not reset person PIN or save their photo")
        return jsonify(error="人員資料儲存失敗，請稍後再試。"), 500

    return (
        jsonify(
            name=name,
            person_id=person_id,
            has_face_sample=image_bytes is not None,
        ),
        result_status,
    )


@app.post("/api/enrollment-requests")
def create_enrollment_request():
    if not _checkin_settings_ready():
        return jsonify(error="管理員審核尚未完成伺服器設定。"), 503
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    name = payload.get("name")
    pin = payload.get("pin")
    if not isinstance(name, str):
        return jsonify(error="請輸入有效的人名。"), 400
    name = _normalize_name(name)
    if not name or len(name) > 80:
        return jsonify(error="人名不可空白且不得超過 80 個字元。"), 400
    if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
        return jsonify(error="PIN 必須為 6 至 12 位數字。"), 400
    if payload.get("face_image_consent") is not True:
        return jsonify(error="提交人臉照片前必須確認已取得本人明確同意。"), 400

    image_bytes, content_type, image_error = _decode_face_image(
        payload.get("face_image_base64")
    )
    if image_error:
        return jsonify(error=image_error[0]), image_error[1]

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    salt, pin_hash = _hash_pin(pin)
    try:
        with closing(_connect_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM people WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone():
                return jsonify(error="此姓名已完成登記，請洽管理員。"), 409
            if connection.execute(
                "SELECT 1 FROM enrollment_requests WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone():
                return jsonify(error="此姓名已有待審申請，請勿重複提交。"), 409

            cursor = connection.execute(
                "INSERT INTO enrollment_requests "
                "(name, pin_salt, pin_hash, image, content_type, created_at, "
                "consented_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    salt,
                    pin_hash,
                    image_bytes,
                    content_type,
                    created_at,
                    created_at,
                ),
            )
            request_id = cursor.lastrowid
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not save pending enrollment request")
        return jsonify(error="建檔申請無法儲存，請稍後再試。"), 500

    return jsonify(request_id=request_id, name=name, status="pending"), 202


@app.get("/api/enrollment-requests/<int:request_id>/photo")
@_admin_required
def enrollment_request_photo(request_id):
    try:
        with closing(_connect_db()) as connection:
            photo = connection.execute(
                "SELECT image, content_type FROM enrollment_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
    except sqlite3.Error:
        app.logger.exception("Could not read pending enrollment photo")
        return jsonify(error="無法讀取待審照片，請稍後再試。"), 500
    if photo is None:
        return jsonify(error="找不到此建檔申請。"), 404
    return Response(
        photo["image"],
        mimetype=photo["content_type"],
        headers={"Cache-Control": "no-store, private"},
    )


@app.post("/api/enrollment-requests/<int:request_id>/approve")
@_admin_required
def approve_enrollment_request(request_id):
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    try:
        with closing(_connect_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            enrollment = connection.execute(
                "SELECT * FROM enrollment_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if enrollment is None:
                connection.rollback()
                return jsonify(error="找不到此建檔申請。"), 404
            if connection.execute(
                "SELECT 1 FROM people WHERE name = ? COLLATE NOCASE",
                (enrollment["name"],),
            ).fetchone():
                connection.rollback()
                return jsonify(error="此姓名已登記，請先處理既有人員資料。"), 409

            cursor = connection.execute(
                "INSERT INTO people (name, pin_salt, pin_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    enrollment["name"],
                    enrollment["pin_salt"],
                    enrollment["pin_hash"],
                    created_at,
                ),
            )
            person_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO face_samples "
                "(person_id, image, content_type, created_at, consented_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    person_id,
                    enrollment["image"],
                    enrollment["content_type"],
                    enrollment["created_at"],
                    enrollment["consented_at"],
                ),
            )
            connection.execute(
                "DELETE FROM enrollment_requests WHERE id = ?", (request_id,)
            )
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not approve pending enrollment request")
        return jsonify(error="核准建檔失敗，申請仍保留待審。"), 500

    return (
        jsonify(name=enrollment["name"], person_id=person_id, has_face_sample=True),
        201,
    )


@app.delete("/api/enrollment-requests/<int:request_id>")
@_admin_required
def reject_enrollment_request(request_id):
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    try:
        with closing(_connect_db()) as connection:
            cursor = connection.execute(
                "DELETE FROM enrollment_requests WHERE id = ?", (request_id,)
            )
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not delete rejected enrollment request")
        return jsonify(error="拒絕申請失敗，請稍後再試。"), 500

    if cursor.rowcount == 0:
        return jsonify(error="找不到此建檔申請。"), 404
    return jsonify(deleted=True)


@app.delete("/api/people/<int:person_id>/face-photo")
@_admin_required
def delete_face_photo(person_id):
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    try:
        with closing(_connect_db()) as connection:
            cursor = connection.execute(
                "DELETE FROM face_samples WHERE person_id = ?", (person_id,)
            )
            deleted = cursor.rowcount > 0
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not delete SQLite face photo")
        return jsonify(error="人臉照片刪除失敗，請稍後再試。"), 500

    return jsonify(deleted=deleted)


@app.post("/api/checkins")
@_employee_required
def create_checkin():
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    person_id = session["checkin_person_id"]
    checked_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )

    try:
        with closing(_connect_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            person = connection.execute(
                "SELECT id, name, pin_hash FROM people WHERE id = ?",
                (person_id,),
            ).fetchone()
            if person is None or not hmac.compare_digest(
                person["pin_hash"] or "",
                session["checkin_pin_hash"],
            ):
                session.pop("checkin_person_id", None)
                session.pop("checkin_pin_hash", None)
                return jsonify(error="此人員已不存在，請重新登入。"), 401
            name = person["name"]
            connection.execute(
                'INSERT INTO mytable (person_id, "姓名", "打卡時間") '
                "VALUES (?, ?, ?)",
                (person_id, name, checked_at),
            )
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not save SQLite check-in")
        return jsonify(error="打卡資料儲存失敗，請稍後再試。"), 500

    return jsonify(name=name, checkin_time=checked_at), 201


@app.errorhandler(413)
def request_too_large(_):
    return jsonify(error="請求大小超過限制。"), 413


if __name__ == "__main__":
    app.run(debug=True)
