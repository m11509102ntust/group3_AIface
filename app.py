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

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY") or None
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("WEBSITE_SITE_NAME"))

FACE_DETECT_URL = "https://api-face-detect.knowlez.com/v1/face/detect"
AZURE_FACE_API_VERSION = "v1.0"
AZURE_FACE_PERSON_GROUP_ID = "facesense-poc"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FACE_DETECT_RATE_LIMIT = 10
FACE_DETECT_RATE_WINDOW = 60
_face_detect_requests = defaultdict(deque)
_face_detect_rate_lock = threading.Lock()
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
            face_image BLOB,
            face_enrolled_at TEXT,
            azure_person_id TEXT,
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
    if "face_image" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN face_image BLOB")
    if "face_enrolled_at" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN face_enrolled_at TEXT")
    if "azure_person_id" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN azure_person_id TEXT")
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
        CREATE TABLE IF NOT EXISTS app_settings (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO app_settings (name, value) "
        "VALUES ('face_detection_enabled', 'true')"
    )
    connection.execute(
        'CREATE INDEX IF NOT EXISTS idx_mytable_checkin_time ON mytable("打卡時間")'
    )
    connection.commit()
    return connection


def _face_detection_enabled(connection):
    setting = connection.execute(
        "SELECT value FROM app_settings WHERE name = 'face_detection_enabled'"
    ).fetchone()
    return setting is None or setting["value"] == "true"


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


def _azure_face_configured():
    return bool(
        os.getenv("AZURE_FACE_ENDPOINT", "").strip()
        and os.getenv("AZURE_FACE_KEY", "").strip()
    )


def _azure_face_url(path):
    endpoint = os.getenv("AZURE_FACE_ENDPOINT", "").strip().rstrip("/")
    return f"{endpoint}/face/{AZURE_FACE_API_VERSION}/{path.lstrip('/')}"


def _decode_image_base64(image_base64):
    if not isinstance(image_base64, str) or not image_base64:
        return None, "請提供 Base64 格式的影像。"
    if len(image_base64) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        return None, "影像大小不可超過 5 MB。"
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError):
        return None, "影像資料不是有效的 Base64。"
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return None, "影像大小不可超過 5 MB。"
    if not (
        image_bytes.startswith(b"\xff\xd8\xff")
        or image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        return None, "影像格式必須是 JPEG 或 PNG。"
    return image_bytes, None


def _azure_detect_face(image_bytes):
    response = requests.post(
        _azure_face_url(
            "detect?returnFaceId=true&returnFaceLandmarks=false"
            "&detectionModel=detection_03&recognitionModel=recognition_04"
        ),
        headers={
            "Ocp-Apim-Subscription-Key": os.getenv("AZURE_FACE_KEY", "").strip(),
            "Content-Type": "application/octet-stream",
        },
        data=image_bytes,
        timeout=(5, 30),
        allow_redirects=False,
    )
    if response.status_code not in (200, 201):
        app.logger.warning("Azure Face detect returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face 偵測失敗。")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Azure Face 回傳了無效資料。") from exc
    if not isinstance(result, list):
        raise RuntimeError("Azure Face 回傳格式不正確。")
    return result


def _azure_verify_faces(face_id_1, face_id_2):
    response = requests.post(
        _azure_face_url("verify"),
        headers={
            "Ocp-Apim-Subscription-Key": os.getenv("AZURE_FACE_KEY", "").strip(),
            "Content-Type": "application/json",
        },
        json={"faceId1": face_id_1, "faceId2": face_id_2},
        timeout=(5, 30),
        allow_redirects=False,
    )
    if response.status_code not in (200, 201):
        app.logger.warning("Azure Face verify returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face 比對失敗。")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Azure Face 回傳了無效資料。") from exc
    return result


def _azure_request(method, path, *, data=None, json=None):
    headers = {
        "Ocp-Apim-Subscription-Key": os.getenv("AZURE_FACE_KEY", "").strip()
    }
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    elif json is not None:
        headers["Content-Type"] = "application/json"
    response = requests.request(
        method,
        _azure_face_url(path),
        headers=headers,
        data=data,
        json=json,
        timeout=(5, 30),
        allow_redirects=False,
    )
    return response


def _azure_ensure_person_group():
    response = _azure_request("GET", f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}")
    if response.status_code == 404:
        response = _azure_request(
            "PUT",
            f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}",
            json={
                "name": "FaceSense POC",
                "userData": "FaceSense Azure Face identity group",
                "recognitionModel": "recognition_04",
            },
        )
    if response.status_code not in (200, 201):
        app.logger.warning("Azure Face person group returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face Person Group 建立或讀取失敗。")


def _azure_create_person(name):
    response = _azure_request(
        "POST",
        f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/persons",
        json={"name": name},
    )
    if response.status_code not in (200, 201):
        app.logger.warning("Azure Face person creation returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face Person 建立失敗。")
    try:
        return response.json()["personId"]
    except (ValueError, KeyError) as exc:
        raise RuntimeError("Azure Face Person 回傳格式不正確。") from exc


def _azure_add_person_face(person_id, image_bytes):
    response = _azure_request(
        "POST",
        f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/persons/{person_id}/persistedFaces",
        data=image_bytes,
    )
    if response.status_code not in (200, 201):
        app.logger.warning("Azure Face persisted face returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face 人臉照片新增失敗。")


def _azure_train_person_group():
    response = _azure_request(
        "POST",
        f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/train",
    )
    if response.status_code not in (202, 200):
        app.logger.warning("Azure Face training returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face Person Group 訓練啟動失敗。")


def _azure_training_status():
    response = _azure_request(
        "GET",
        f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/training",
    )
    if response.status_code != 200:
        app.logger.warning("Azure Face training status returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face 訓練狀態讀取失敗。")
    try:
        return response.json().get("status")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("Azure Face 訓練狀態格式不正確。") from exc


def _azure_identify(image_bytes):
    faces = _azure_detect_face(image_bytes)
    if len(faces) != 1 or not faces[0].get("faceId"):
        raise ValueError("畫面必須且只能包含一張清楚的人臉。")
    training_status = _azure_training_status()
    if training_status != "succeeded":
        raise RuntimeError("Azure Face 人臉資料尚未訓練完成，請稍後再試。")
    response = _azure_request(
        "POST",
        "identify",
        json={
            "faceIds": [faces[0]["faceId"]],
            "personGroupId": AZURE_FACE_PERSON_GROUP_ID,
            "maxNumOfCandidatesReturned": 1,
            "confidenceThreshold": 0.6,
        },
    )
    if response.status_code not in (200, 201):
        app.logger.warning("Azure Face identify returned HTTP %s", response.status_code)
        raise RuntimeError("Azure Face 身分辨識失敗。")
    try:
        results = response.json()
    except ValueError as exc:
        raise RuntimeError("Azure Face 身分辨識回傳格式不正確。") from exc
    if not results or not results[0].get("candidates"):
        return None
    return results[0]["candidates"][0]


@app.get("/")
@app.get("/checkin")
@app.get("/records")
@app.get("/people")
@app.get("/registered")
@app.get("/settings")
def index():
    checkin_password_configured = bool(
        os.getenv("CHECKIN_ADMIN_PASSWORD", "").strip()
    )
    flask_secret_configured = bool(app.secret_key)
    checkin_configured = checkin_password_configured and flask_secret_configured
    face_detection_configured = bool(os.getenv("FACE_DETECT_API_KEY", "").strip())
    face_recognition_configured = _azure_face_configured()
    face_detection_enabled = True
    admin_authenticated = bool(
        checkin_configured and session.get("checkin_admin_authenticated")
    )
    employee_id = session.get("checkin_person_id") if checkin_configured else None
    employee_authenticated = isinstance(employee_id, int)
    people = []
    checkins = []
    employee_name = None
    employee_face_enrolled = False
    database_error = None

    dashboard_metrics = None
    database_ready = False
    try:
        with closing(_connect_db()) as connection:
            database_ready = True
            face_detection_enabled = _face_detection_enabled(connection)
            if admin_authenticated:
                people = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT name, pin_hash IS NOT NULL AS has_pin, "
                        "azure_person_id IS NOT NULL AS has_face "
                        "FROM people ORDER BY name COLLATE NOCASE"
                    )
                ]
            if employee_authenticated:
                person = connection.execute(
                    "SELECT name, pin_hash, azure_person_id IS NOT NULL AS has_face "
                    "FROM people WHERE id = ?",
                    (employee_id,),
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
                    employee_face_enrolled = bool(person["has_face"])

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

    page = request.path.strip("/") or "overview"
    if page in {"people", "registered", "settings"} and not admin_authenticated:
        return redirect("/")

    return render_template(
        "index.html",
        page=page,
        face_detection_configured=face_detection_configured,
        face_recognition_configured=face_recognition_configured,
        face_detection_enabled=face_detection_enabled,
        checkin_configured=checkin_configured,
        checkin_password_configured=checkin_password_configured,
        flask_secret_configured=flask_secret_configured,
        database_ready=database_ready,
        admin_authenticated=admin_authenticated,
        employee_authenticated=employee_authenticated,
        employee_name=employee_name,
        employee_face_enrolled=employee_authenticated and employee_face_enrolled,
        dashboard_metrics=dashboard_metrics,
        csrf_token=_csrf_token() if checkin_configured else "",
        people=people,
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
        return redirect("/?admin_login_error=2")
    if not hmac.compare_digest(supplied_password, expected_password):
        return redirect("/?admin_login_error=1")

    _clear_pin_login_attempts("admin:login")
    session.clear()
    session["checkin_admin_authenticated"] = True
    _csrf_token()
    return redirect("/")


@app.post("/person-login")
def person_login():
    if not _checkin_settings_ready():
        return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
    if not _csrf_is_valid():
        return "CSRF 驗證失敗，請重新載入頁面。", 400

    name = _normalize_name(request.form.get("name", ""))
    pin = request.form.get("pin", "")
    if not name or len(name) > 80 or not PIN_PATTERN.fullmatch(pin):
        return redirect("/?person_login_error=1")
    if not _allow_pin_login(name):
        return redirect("/?person_login_error=2")

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
        return redirect("/?person_login_error=1")

    _clear_pin_login_attempts(name)
    session.clear()
    session["checkin_person_id"] = person["id"]
    session["checkin_pin_hash"] = person["pin_hash"]
    _csrf_token()
    return redirect("/")


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
    return redirect("/")


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

    confirm_new_person = payload.get("confirm_new_person") is True
    confirm_pin_reset = payload.get("confirm_pin_reset") is True
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    salt, pin_hash = _hash_pin(pin)

    try:
        with closing(_connect_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            person = connection.execute(
                "SELECT id, name, azure_person_id FROM people "
                "WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()

            if person is None and not confirm_new_person:
                return jsonify(
                    needs_confirmation=True,
                    name=name,
                    message=f"「{name}」尚未登記，是否新增人員並設定此 PIN？",
                ), 409
            if person is not None and not confirm_pin_reset:
                return jsonify(
                    needs_reset_confirmation=True,
                    name=person["name"],
                    message=f"「{person['name']}」已存在，是否重設其 PIN？",
                ), 409

            if person is None:
                connection.execute(
                    "INSERT INTO people (name, pin_salt, pin_hash, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, salt, pin_hash, created_at),
                )
                result_status = 201
            else:
                name = person["name"]
                connection.execute(
                    "UPDATE people SET pin_salt = ?, pin_hash = ? WHERE id = ?",
                    (salt, pin_hash, person["id"]),
                )
                result_status = 200
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not register person or update PIN")
        return jsonify(error="人員資料儲存失敗，請稍後再試。"), 500

    return jsonify(name=name), result_status


@app.post("/api/people/face")
@_admin_required
def enroll_person_face():
    if not _csrf_is_valid():
        return jsonify(error="CSRF 驗證失敗，請重新載入頁面。"), 400
    if not _azure_face_configured():
        return jsonify(error="尚未設定 Azure Face Endpoint 與 API 金鑰。"), 503

    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    name = payload.get("name")
    image_bytes, image_error = _decode_image_base64(payload.get("image_base64"))
    if not isinstance(name, str) or not _normalize_name(name):
        return jsonify(error="請提供已登記的人員姓名。"), 400
    if image_error:
        return jsonify(error=image_error), 400
    name = _normalize_name(name)

    try:
        with closing(_connect_db()) as connection:
            person = connection.execute(
                "SELECT id, name, azure_person_id FROM people "
                "WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if person is None:
                return jsonify(error="請先建立人員與 PIN，再登記人臉。"), 404

        _azure_ensure_person_group()
        azure_person_id = person["azure_person_id"] or _azure_create_person(person["name"])
        _azure_add_person_face(azure_person_id, image_bytes)
        _azure_train_person_group()

        enrolled_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        with closing(_connect_db()) as connection:
            connection.execute(
                "UPDATE people SET azure_person_id = ?, face_image = NULL, "
                "face_enrolled_at = ? WHERE id = ?",
                (azure_person_id, enrolled_at, person["id"]),
            )
            connection.commit()
    except requests.RequestException:
        app.logger.exception("Azure Face enrollment request failed")
        return jsonify(error="無法連線到 Azure Face，請稍後再試。"), 502
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502
    except sqlite3.Error:
        app.logger.exception("Could not save enrolled face")
        return jsonify(error="人臉資料儲存失敗，請稍後再試。"), 500

    return jsonify(name=person["name"], face_enrolled=True), 201


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


@app.post("/api/face-checkins")
@_employee_required
def create_face_checkin():
    if not _csrf_is_valid():
        return jsonify(error="CSRF 驗證失敗，請重新載入頁面。"), 400
    if not _azure_face_configured():
        return jsonify(error="尚未設定 Azure Face Endpoint 與 API 金鑰。"), 503

    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    image_bytes, image_error = _decode_image_base64(payload.get("image_base64"))
    if image_error:
        return jsonify(error=image_error), 400

    try:
        with closing(_connect_db()) as connection:
            person = connection.execute(
                "SELECT id, name, azure_person_id FROM people WHERE id = ?",
                (session["checkin_person_id"],),
            ).fetchone()
        if person is None or not person["azure_person_id"]:
            return jsonify(error="尚未登記人臉，請洽管理員。"), 409

        candidate = _azure_identify(image_bytes)
        if candidate is None or candidate.get("personId") != person["azure_person_id"]:
            return jsonify(error="人臉比對不相符，請改用 PIN 打卡。"), 403

        checked_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        with closing(_connect_db()) as connection:
            connection.execute(
                'INSERT INTO mytable (person_id, "姓名", "打卡時間") VALUES (?, ?, ?)',
                (person["id"], person["name"], checked_at),
            )
            connection.commit()
    except requests.RequestException:
        app.logger.exception("Azure Face check-in request failed")
        return jsonify(error="無法連線到 Azure Face，請稍後再試。"), 502
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502
    except sqlite3.Error:
        app.logger.exception("Could not save face check-in")
        return jsonify(error="打卡資料儲存失敗，請稍後再試。"), 500

    return jsonify(
        name=person["name"],
        checkin_time=checked_at,
        confidence=candidate.get("confidence", 0),
    ), 201


@app.post("/api/settings/face-detection")
@_admin_required
def update_face_detection_setting():
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400
    if not os.getenv("FACE_DETECT_API_KEY", "").strip():
        return jsonify(error="尚未設定人臉偵測 API 金鑰，無法啟用此功能。"), 409

    payload = request.get_json(silent=True)
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return jsonify(error="設定值必須是布林值。"), 400

    try:
        with closing(_connect_db()) as connection:
            connection.execute(
                "UPDATE app_settings SET value = ? "
                "WHERE name = 'face_detection_enabled'",
                ("true" if enabled else "false",),
            )
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not update face detection setting")
        return jsonify(error="系統設定儲存失敗，請稍後再試。"), 500

    return jsonify(enabled=enabled)


@app.post("/api/face/detect")
def detect_face():
    if not _allow_face_detection(request.remote_addr or "unknown"):
        return jsonify(error="偵測次數過多，請稍後再試。"), 429

    try:
        with closing(_connect_db()) as connection:
            if not _face_detection_enabled(connection):
                return jsonify(error="人臉偵測功能目前由管理員停用。"), 503
    except sqlite3.Error:
        app.logger.exception("Could not load face detection setting")
        return jsonify(error="目前無法讀取人臉偵測設定。"), 503

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
