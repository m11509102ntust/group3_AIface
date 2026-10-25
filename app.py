import base64
import binascii
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY") or None
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("WEBSITE_SITE_NAME"))

FACE_DETECT_URL = "https://api-face-detect.knowlez.com/v1/face/detect"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FACE_DETECT_RATE_LIMIT = 10
FACE_DETECT_RATE_WINDOW = 60
_face_detect_requests = defaultdict(deque)
_face_detect_rate_lock = threading.Lock()


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
            created_at TEXT NOT NULL
        )
        """
    )
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
        'CREATE INDEX IF NOT EXISTS idx_mytable_checkin_time ON mytable("打卡時間")'
    )
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


def _login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not _checkin_settings_ready():
            return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
        if not session.get("checkin_authenticated"):
            if request.path.startswith("/api/"):
                return jsonify(error="請先登入再使用打卡功能。"), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped_view


def _allow_face_detection(client_ip):
    now = time.monotonic()
    with _face_detect_rate_lock:
        for ip, timestamps in list(_face_detect_requests.items()):
            while timestamps and now - timestamps[0] >= FACE_DETECT_RATE_WINDOW:
                timestamps.popleft()
            if not timestamps:
                del _face_detect_requests[ip]

        timestamps = _face_detect_requests[client_ip]
        if len(timestamps) >= FACE_DETECT_RATE_LIMIT:
            return False
        timestamps.append(now)
        return True


@app.get("/")
def index():
    checkin_authenticated = bool(
        _checkin_settings_ready() and session.get("checkin_authenticated")
    )
    people = []
    checkins = []
    database_error = None

    if checkin_authenticated:
        try:
            with closing(_connect_db()) as connection:
                people = [
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM people ORDER BY name COLLATE NOCASE"
                    )
                ]
                checkins = [
                    dict(row)
                    for row in connection.execute(
                        'SELECT "姓名" AS name, "打卡時間" AS checkin_time '
                        "FROM mytable ORDER BY id DESC LIMIT 50"
                    )
                ]
        except sqlite3.Error:
            app.logger.exception("Could not load SQLite check-in data")
            database_error = "無法讀取打卡資料庫，請稍後再試。"

    return render_template(
        "index.html",
        face_detection_configured=bool(os.getenv("FACE_DETECT_API_KEY", "").strip()),
        checkin_configured=_checkin_settings_ready(),
        checkin_authenticated=checkin_authenticated,
        csrf_token=_csrf_token() if _checkin_settings_ready() else "",
        people=people,
        checkins=checkins,
        database_error=database_error,
        login_error=None,
    )


@app.post("/login")
def login():
    if not _checkin_settings_ready():
        return redirect(url_for("index"))
    if not _csrf_is_valid():
        return "CSRF 驗證失敗，請重新載入頁面。", 400

    supplied_password = request.form.get("password", "")
    expected_password = os.environ["CHECKIN_ADMIN_PASSWORD"]
    if not hmac.compare_digest(supplied_password, expected_password):
        return render_template(
            "index.html",
            face_detection_configured=bool(
                os.getenv("FACE_DETECT_API_KEY", "").strip()
            ),
            checkin_configured=True,
            checkin_authenticated=False,
            csrf_token=_csrf_token(),
            people=[],
            checkins=[],
            database_error=None,
            login_error="密碼不正確，請重試。",
        ), 401

    session.clear()
    session["checkin_authenticated"] = True
    _csrf_token()
    return redirect(url_for("index"))


@app.post("/logout")
@_login_required
def logout():
    if not _csrf_is_valid():
        return "CSRF 驗證失敗，請重新載入頁面。", 400
    session.clear()
    return redirect(url_for("index"))


@app.post("/api/checkins")
@_login_required
def create_checkin():
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400

    payload = request.get_json(silent=True)
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str):
        return jsonify(error="請輸入有效的人名。"), 400

    name = " ".join(name.split())
    if not name or len(name) > 80:
        return jsonify(error="人名不可空白且不得超過 80 個字元。"), 400

    confirm_new_person = (
        isinstance(payload, dict) and payload.get("confirm_new_person") is True
    )
    checked_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )

    try:
        with closing(_connect_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            person = connection.execute(
                "SELECT id, name FROM people WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()

            if person is None and not confirm_new_person:
                return jsonify(
                    needs_confirmation=True,
                    name=name,
                    message=f"「{name}」尚未登記，是否新增人員並完成本次打卡？",
                ), 409

            if person is None:
                cursor = connection.execute(
                    "INSERT INTO people (name, created_at) VALUES (?, ?)",
                    (name, checked_at),
                )
                person_id = cursor.lastrowid
            else:
                person_id = person["id"]
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


@app.post("/api/face/detect")
def detect_face():
    if not _allow_face_detection(request.remote_addr or "unknown"):
        return jsonify(error="偵測次數過多，請稍後再試。"), 429

    api_key = os.getenv("FACE_DETECT_API_KEY", "").strip()
    if not api_key:
        return jsonify(error="人臉偵測服務尚未設定 API 金鑰。"), 503

    payload = request.get_json(silent=True)
    image_base64 = payload.get("image_base64") if isinstance(payload, dict) else None
    if not isinstance(image_base64, str) or not image_base64:
        return jsonify(error="請提供 Base64 格式的 image_base64。"), 400

    if len(image_base64) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        return jsonify(error="圖片大小不可超過 5 MB。"), 413

    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError):
        return jsonify(error="圖片必須是有效的 Base64 編碼。"), 400

    if len(image_bytes) > MAX_IMAGE_BYTES:
        return jsonify(error="圖片大小不可超過 5 MB。"), 413
    if not (
        image_bytes.startswith(b"\xff\xd8\xff")
        or image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        return jsonify(error="圖片格式只支援 JPEG 或 PNG。"), 400

    try:
        response = requests.post(
            FACE_DETECT_URL,
            headers={"x-api-key": api_key},
            json={"image_base64": image_base64},
            timeout=(5, 30),
            allow_redirects=False,
        )
    except requests.Timeout:
        app.logger.warning("Face detection provider timed out")
        return jsonify(error="人臉偵測服務逾時，請稍後重試。"), 504
    except requests.RequestException as exc:
        app.logger.warning(
            "Face detection provider request failed: %s", type(exc).__name__
        )
        return jsonify(error="目前無法連線到人臉偵測服務。"), 502

    if response.status_code not in (200, 201):
        app.logger.warning(
            "Face detection provider returned HTTP %s", response.status_code
        )
        return jsonify(error="人臉偵測服務無法處理這張圖片。"), 502

    try:
        result = response.json()
    except ValueError:
        app.logger.warning("Face detection provider returned invalid JSON")
        return jsonify(error="人臉偵測服務回傳了無效資料。"), 502

    return jsonify(result)


@app.errorhandler(413)
def request_too_large(_):
    return jsonify(error="請求大小超過限制。"), 413


if __name__ == "__main__":
    app.run(debug=True)
