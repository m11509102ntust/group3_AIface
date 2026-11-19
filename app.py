import base64
import binascii
import hashlib
import hmac
import json
import math
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

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY") or None
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("WEBSITE_SITE_NAME"))

FACE_DETECT_URL = "https://api-face-detect.knowlez.com/v1/face/detect"
AZURE_FACE_API_VERSION = "v1.0"
AZURE_FACE_PERSON_GROUP_ID = "facesense-production"
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
    if "face_embedding" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN face_embedding TEXT")
    if "face_enrolled_at" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN face_enrolled_at TEXT")
    if "azure_person_id" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN azure_person_id TEXT")
    if "face_image" not in people_columns:
        connection.execute("ALTER TABLE people ADD COLUMN face_image BLOB")
    for legacy_column in ("embedding", "face_encoding", "face_descriptor", "face_vector"):
        if legacy_column in people_columns:
            connection.execute(
                f"UPDATE people SET face_embedding = {legacy_column} "
                "WHERE face_embedding IS NULL"
            )
            break
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


def _extract_face_embedding(result):
    """Return a finite numeric embedding from the provider response."""
    candidates = []
    if isinstance(result, dict):
        candidates.append(result)
        faces = result.get("faces")
        if isinstance(faces, list):
            candidates.extend(face for face in faces if isinstance(face, dict))
    for candidate in candidates:
        for key in ("embedding", "face_embedding", "encoding", "vector"):
            value = candidate.get(key)
            if not isinstance(value, list) or len(value) < 2:
                continue
            try:
                embedding = [float(item) for item in value]
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(item) for item in embedding):
                return embedding
    return None


def _cosine_similarity(left, right):
    if len(left) != len(right):
        return None
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return None
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _local_face_available():
    return cv2 is not None and np is not None


def _local_model_dir():
    if os.getenv("WEBSITE_SITE_NAME"):
        model_dir = Path(os.getenv("HOME", r"D:\home")) / "data" / "facesense"
    else:
        model_dir = Path(app.instance_path) / "facesense"
    model_dir.mkdir(parents=True, exist_ok=True)
    return model_dir


def _extract_local_face(image_bytes):
    if not _local_face_available():
        return None, "本機人臉辨識元件尚未安裝。請安裝 numpy 與 opencv-contrib-python。"
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None, "照片無法解碼。"
    image = cv2.equalizeHist(image)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    faces = detector.detectMultiScale(image, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    if len(faces) != 1:
        return None, "畫面必須且只能包含一張清楚的人臉。"
    x, y, width, height = max(faces, key=lambda face: face[2] * face[3])
    face = cv2.resize(image[y:y + height, x:x + width], (200, 200))
    return face, None


def _train_local_face_model():
    if not _local_face_available():
        return None, "本機人臉辨識元件尚未安裝。"
    with closing(_connect_db()) as connection:
        people = connection.execute(
            "SELECT id, name, face_image FROM people "
            "WHERE face_image IS NOT NULL"
        ).fetchall()
    faces = []
    labels = []
    mapping = {}
    for person in people:
        face, error = _extract_local_face(person["face_image"])
        if error:
            continue
        label = len(mapping)
        mapping[str(label)] = {"id": person["id"], "name": person["name"]}
        faces.append(face)
        labels.append(label)
    if not faces:
        return None, "尚未建立可用的人臉模型。"
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.train(faces, np.asarray(labels, dtype=np.int32))
    model_dir = _local_model_dir()
    recognizer.save(str(model_dir / "lbph-model.yml"))
    (model_dir / "lbph-labels.json").write_text(
        json.dumps(mapping, ensure_ascii=False),
        encoding="utf-8",
    )
    return mapping, None


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
    provider = os.getenv("FACE_RECOGNITION_PROVIDER", "local").strip().lower()
    face_detection_configured = (
        provider == "local"
        and _local_face_available()
    ) or bool(
        os.getenv("FACE_DETECT_API_KEY", "").strip()
        or _azure_face_configured()
    )
    face_detection_enabled = True
    admin_authenticated = bool(
        checkin_configured and session.get("checkin_admin_authenticated")
    )
    employee_id = session.get("checkin_person_id") if checkin_configured else None
    employee_authenticated = isinstance(employee_id, int)
    people = []
    checkins = []
    employee_name = None
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
                        "SELECT id, name, pin_hash IS NOT NULL AS has_pin, "
                        "face_embedding IS NOT NULL AS has_face_model "
                        "FROM people ORDER BY name COLLATE NOCASE"
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

    page = request.path.strip("/") or "overview"
    if page in {"people", "registered", "settings"} and not admin_authenticated:
        return redirect("/")

    return render_template(
        "index.html",
        page=page,
        face_detection_configured=face_detection_configured,
        face_detection_enabled=face_detection_enabled,
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
                "SELECT id, name FROM people WHERE name = ? COLLATE NOCASE",
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


def _request_face_embedding(image_base64):
    api_key = os.getenv("FACE_DETECT_API_KEY", "").strip()
    if not api_key:
        return None, "人臉辨識服務尚未設定 API 金鑰。", 503
    try:
        response = requests.post(
            FACE_DETECT_URL,
            headers={"x-api-key": api_key},
            json={"image_base64": image_base64},
            timeout=(5, 30),
            allow_redirects=False,
        )
    except requests.Timeout:
        app.logger.warning("Face recognition provider timed out")
        return None, "人臉辨識服務逾時，請稍後重試。", 504
    except requests.RequestException as exc:
        app.logger.warning(
            "Face recognition provider request failed: %s", type(exc).__name__
        )
        return None, "目前無法連線到人臉辨識服務。", 502

    if response.status_code not in (200, 201):
        app.logger.warning(
            "Face recognition provider returned HTTP %s", response.status_code
        )
        return None, "人臉辨識服務無法處理這張圖片。", 502
    try:
        result = response.json()
    except ValueError:
        app.logger.warning("Face recognition provider returned invalid JSON")
        return None, "人臉辨識服務回傳了無效資料。", 502

    embedding = _extract_face_embedding(result)
    if embedding is None:
        return (
            None,
            "人臉服務未回傳 embedding；目前的 API 只有位置偵測，無法建立或比對模型。",
            422,
        )
    return embedding, None, None


def _azure_face_configured():
    return bool(
        os.getenv("AZURE_FACE_ENDPOINT", "").strip()
        and os.getenv("AZURE_FACE_KEY", "").strip()
    )


def _azure_face_url(path):
    endpoint = os.getenv("AZURE_FACE_ENDPOINT", "").strip().rstrip("/")
    return f"{endpoint}/face/{AZURE_FACE_API_VERSION}/{path.lstrip('/')}"


def _azure_request(method, path, *, data=None, json=None):
    headers = {
        "Ocp-Apim-Subscription-Key": os.getenv("AZURE_FACE_KEY", "").strip()
    }
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    elif json is not None:
        headers["Content-Type"] = "application/json"
    return requests.request(
        method,
        _azure_face_url(path),
        headers=headers,
        data=data,
        json=json,
        timeout=(5, 30),
        allow_redirects=False,
    )


def _azure_ensure_person_group():
    response = _azure_request(
        "GET", f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}"
    )
    if response.status_code == 404:
        response = _azure_request(
            "PUT",
            f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}",
            json={
                "name": "FaceSense",
                "userData": "FaceSense Azure Face identity group",
                "recognitionModel": "recognition_04",
            },
        )
    if response.status_code not in (200, 201):
        app.logger.warning(
            "Azure Face person group returned HTTP %s", response.status_code
        )
        raise RuntimeError("Azure Face Person Group 建立或讀取失敗。")


def _azure_create_person(name):
    response = _azure_request(
        "POST",
        f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/persons",
        json={"name": name},
    )
    if response.status_code not in (200, 201):
        app.logger.warning(
            "Azure Face person creation returned HTTP %s", response.status_code
        )
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
        app.logger.warning(
            "Azure Face persisted face returned HTTP %s", response.status_code
        )
        raise RuntimeError("Azure Face 人臉照片新增失敗。")


def _azure_train_person_group():
    response = _azure_request(
        "POST", f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/train"
    )
    if response.status_code not in (200, 202):
        app.logger.warning(
            "Azure Face training returned HTTP %s", response.status_code
        )
        raise RuntimeError("Azure Face Person Group 訓練啟動失敗。")


def _azure_training_status():
    response = _azure_request(
        "GET", f"persongroups/{AZURE_FACE_PERSON_GROUP_ID}/training"
    )
    if response.status_code != 200:
        raise RuntimeError("Azure Face 訓練狀態讀取失敗。")
    try:
        return response.json().get("status")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("Azure Face 訓練狀態格式不正確。") from exc


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


def _azure_identify(image_bytes):
    faces = _azure_detect_face(image_bytes)
    if len(faces) != 1 or not faces[0].get("faceId"):
        raise ValueError("畫面必須且只能包含一張清楚的人臉。")
    if _azure_training_status() != "succeeded":
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


def _find_photo_blob_column(connection):
    photo_columns = {
        "photo",
        "photo_blob",
        "image",
        "image_blob",
        "face_photo",
        "avatar",
        "picture",
    }
    columns = connection.execute("PRAGMA table_info(people)").fetchall()
    for column in columns:
        if column["name"].casefold() in photo_columns and column["type"].upper() == "BLOB":
            return column["name"]
    return None


@app.post("/api/face/enroll")
@_admin_required
def enroll_face():
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    person_id = payload.get("person_id")
    image_base64 = payload.get("image_base64")
    if not isinstance(person_id, int) or person_id <= 0:
        return jsonify(error="請選擇有效的人員。"), 400
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

    provider = os.getenv("FACE_RECOGNITION_PROVIDER", "local").strip().lower()
    if provider == "local" and not _azure_face_configured():
        if not _local_face_available():
            return jsonify(error="本機人臉辨識元件尚未安裝。請安裝 opencv-contrib-python。"), 503
        face, face_error = _extract_local_face(image_bytes)
        if face_error:
            return jsonify(error=face_error), 422
        enrolled_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        try:
            with closing(_connect_db()) as connection:
                person = connection.execute(
                    "SELECT id, name FROM people WHERE id = ?",
                    (person_id,),
                ).fetchone()
                if person is None:
                    return jsonify(error="找不到指定的人員。"), 404
                connection.execute(
                    "UPDATE people SET face_image = ?, face_enrolled_at = ? "
                    "WHERE id = ?",
                    (image_bytes, enrolled_at, person_id),
                )
                connection.commit()
            mapping, train_error = _train_local_face_model()
            if train_error:
                return jsonify(error=train_error), 422
        except sqlite3.Error:
            app.logger.exception("Could not save local face model")
            return jsonify(error="本機人臉模型儲存失敗，請稍後再試。"), 500
        return jsonify(
            person_id=person_id,
            name=person["name"],
            enrolled_at=enrolled_at,
            provider="local-lbph",
        )

    if _azure_face_configured():
        try:
            with closing(_connect_db()) as connection:
                person = connection.execute(
                    "SELECT id, name, azure_person_id FROM people WHERE id = ?",
                    (person_id,),
                ).fetchone()
            if person is None:
                return jsonify(error="找不到指定的人員。"), 404
            _azure_ensure_person_group()
            azure_person_id = person["azure_person_id"] or _azure_create_person(
                person["name"]
            )
            _azure_add_person_face(azure_person_id, image_bytes)
            _azure_train_person_group()
            enrolled_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
            with closing(_connect_db()) as connection:
                connection.execute(
                    "UPDATE people SET azure_person_id = ?, face_enrolled_at = ? "
                    "WHERE id = ?",
                    (azure_person_id, enrolled_at, person_id),
                )
                connection.commit()
            return jsonify(
                person_id=person_id,
                name=person["name"],
                enrolled_at=enrolled_at,
                provider="azure-face",
            )
        except requests.RequestException:
            app.logger.exception("Azure Face enrollment request failed")
            return jsonify(error="無法連線到 Azure Face，請稍後再試。"), 502
        except (RuntimeError, ValueError) as exc:
            return jsonify(error=str(exc)), 502
        except sqlite3.Error:
            app.logger.exception("Could not save Azure Face enrollment")
            return jsonify(error="人臉模型儲存失敗，請稍後再試。"), 500

    embedding, error, status = _request_face_embedding(image_base64)
    if error:
        return jsonify(error=error), status
    enrolled_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    try:
        with closing(_connect_db()) as connection:
            result = connection.execute(
                "UPDATE people SET face_embedding = ?, face_enrolled_at = ? "
                "WHERE id = ?",
                (json.dumps(embedding, separators=(",", ":")), enrolled_at, person_id),
            )
            if result.rowcount != 1:
                return jsonify(error="找不到指定的人員。"), 404
            person = connection.execute(
                "SELECT name FROM people WHERE id = ?", (person_id,)
            ).fetchone()
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not save face embedding")
        return jsonify(error="人臉模型儲存失敗，請稍後再試。"), 500
    return jsonify(person_id=person_id, name=person["name"], enrolled_at=enrolled_at)


@app.post("/api/face/auto-enroll")
@_admin_required
def auto_enroll_faces():
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400
    provider = os.getenv("FACE_RECOGNITION_PROVIDER", "local").strip().lower()
    if provider == "local" and not _azure_face_configured():
        if not _local_face_available():
            return jsonify(error="本機人臉辨識元件尚未安裝。請安裝 opencv-contrib-python。"), 503
        try:
            with closing(_connect_db()) as connection:
                eligible = connection.execute(
                    "SELECT id, name FROM people WHERE face_image IS NOT NULL"
                ).fetchall()
                for person in eligible:
                    connection.execute(
                        "UPDATE people SET face_enrolled_at = COALESCE("
                        "face_enrolled_at, ?) WHERE id = ?",
                        (
                            datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ).replace("+00:00", "Z"),
                            person["id"],
                        ),
                    )
                connection.commit()
            mapping, train_error = _train_local_face_model()
            if train_error:
                return jsonify(error=train_error, enrolled=0, skipped=0, errors=[]), 422
            return jsonify(
                enrolled=len(mapping),
                skipped=max(0, len(eligible) - len(mapping)),
                errors=[],
            )
        except sqlite3.Error:
            app.logger.exception("Could not auto-train local LBPH face model")
            return jsonify(error="本機人臉模型訓練失敗。"), 500
    if _azure_face_configured():
        try:
            with closing(_connect_db()) as connection:
                photo_column = _find_photo_blob_column(connection)
                if photo_column is None:
                    return jsonify(
                        error="people 表找不到可用的照片 BLOB 欄位。",
                        enrolled=0,
                        skipped=0,
                    ), 409
                people = connection.execute(
                    f'SELECT id, name, "{photo_column}" AS photo '
                    "FROM people WHERE azure_person_id IS NULL "
                    f'AND "{photo_column}" IS NOT NULL'
                ).fetchall()
                if not people:
                    return jsonify(enrolled=0, skipped=0, errors=[]), 200
                _azure_ensure_person_group()
                enrolled = 0
                errors = []
                for person in people:
                    try:
                        image_bytes = person["photo"]
                        if not isinstance(image_bytes, bytes) or not (
                            image_bytes.startswith(b"\xff\xd8\xff")
                            or image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                        ):
                            errors.append(f"{person['name']}: 照片格式錯誤")
                            continue
                        azure_person_id = _azure_create_person(person["name"])
                        _azure_add_person_face(azure_person_id, image_bytes)
                        enrolled_at = datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ).replace("+00:00", "Z")
                        connection.execute(
                            "UPDATE people SET azure_person_id = ?, "
                            "face_enrolled_at = ? WHERE id = ?",
                            (azure_person_id, enrolled_at, person["id"]),
                        )
                        enrolled += 1
                    except (RuntimeError, ValueError, requests.RequestException) as exc:
                        errors.append(f"{person['name']}: {exc}")
                if enrolled:
                    _azure_train_person_group()
                connection.commit()
            return jsonify(enrolled=enrolled, skipped=0, errors=errors)
        except requests.RequestException:
            app.logger.exception("Azure Face auto-enrollment request failed")
            return jsonify(error="無法連線到 Azure Face，請稍後再試。"), 502
        except (RuntimeError, sqlite3.Error) as exc:
            app.logger.exception("Could not auto-enroll Azure Face models")
            return jsonify(error=str(exc)), 500
    try:
        with closing(_connect_db()) as connection:
            photo_column = _find_photo_blob_column(connection)
            if photo_column is None:
                return jsonify(
                    error="people 表找不到可用的照片 BLOB 欄位。",
                    enrolled=0,
                    skipped=0,
                ), 409
            people = connection.execute(
                f'SELECT id, name, "{photo_column}" AS photo '
                "FROM people WHERE face_embedding IS NULL "
                f'AND "{photo_column}" IS NOT NULL'
            ).fetchall()
            enrolled = 0
            skipped = 0
            errors = []
            for person in people:
                image_bytes = person["photo"]
                if not isinstance(image_bytes, bytes) or not image_bytes:
                    skipped += 1
                    continue
                if len(image_bytes) > MAX_IMAGE_BYTES or not (
                    image_bytes.startswith(b"\xff\xd8\xff")
                    or image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    errors.append(f"{person['name']}: 照片格式錯誤或超過 5 MB")
                    continue
                image_base64 = base64.b64encode(image_bytes).decode("ascii")
                embedding, error, _status = _request_face_embedding(image_base64)
                if error:
                    errors.append(f"{person['name']}: {error}")
                    continue
                enrolled_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z")
                connection.execute(
                    "UPDATE people SET face_embedding = ?, face_enrolled_at = ? "
                    "WHERE id = ?",
                    (
                        json.dumps(embedding, separators=(",", ":")),
                        enrolled_at,
                        person["id"],
                    ),
                )
                enrolled += 1
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not auto-enroll face embeddings from SQLite photos")
        return jsonify(error="從 SQLite 照片建立人臉模型失敗。"), 500
    return jsonify(
        enrolled=enrolled,
        skipped=skipped,
        errors=errors,
        photo_column=photo_column,
    )


@app.post("/api/face/recognize")
def recognize_face():
    if not _checkin_settings_ready():
        return jsonify(error="打卡登入尚未完成伺服器設定。"), 503
    if not _csrf_is_valid():
        return jsonify(error="安全驗證失敗，請重新載入頁面。"), 400
    if not _allow_face_detection(request.remote_addr or "unknown"):
        return jsonify(error="辨識次數過多，請稍後再試。"), 429
    try:
        with closing(_connect_db()) as connection:
            if not _face_detection_enabled(connection):
                return jsonify(error="人臉辨識功能目前由管理員停用。"), 503
    except sqlite3.Error:
        app.logger.exception("Could not load face recognition setting")
        return jsonify(error="目前無法讀取人臉辨識設定。"), 503

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
    if len(image_bytes) > MAX_IMAGE_BYTES or not (
        image_bytes.startswith(b"\xff\xd8\xff")
        or image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        return jsonify(error="圖片格式錯誤或大小不可超過 5 MB。"), 400

    provider = os.getenv("FACE_RECOGNITION_PROVIDER", "local").strip().lower()
    if provider == "local" and not _azure_face_configured():
        if not _local_face_available():
            return jsonify(error="本機人臉辨識元件尚未安裝。請安裝 opencv-contrib-python。"), 503
        face, face_error = _extract_local_face(image_bytes)
        if face_error:
            return jsonify(error=face_error), 422
        model_dir = _local_model_dir()
        model_path = model_dir / "lbph-model.yml"
        labels_path = model_dir / "lbph-labels.json"
        if not model_path.exists() or not labels_path.exists():
            return jsonify(error="尚未建立可用的人臉模型。"), 404
        try:
            threshold = float(os.getenv("LOCAL_FACE_DISTANCE_THRESHOLD", "75"))
        except ValueError:
            return jsonify(error="LOCAL_FACE_DISTANCE_THRESHOLD 設定無效。"), 500
        try:
            mapping = json.loads(labels_path.read_text(encoding="utf-8"))
            recognizer = cv2.face.LBPHFaceRecognizer_create()
            recognizer.read(str(model_path))
            label, distance = recognizer.predict(face)
            person_info = mapping.get(str(label))
            if person_info is None or distance > threshold:
                return jsonify(
                    error="找不到相符的人員。",
                    distance=round(float(distance), 4),
                ), 404
            checked_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
            with closing(_connect_db()) as connection:
                person = connection.execute(
                    "SELECT id, name FROM people WHERE id = ?",
                    (person_info["id"],),
                ).fetchone()
                if person is None:
                    return jsonify(error="辨識結果不在目前人員名冊中。"), 404
                connection.execute(
                    'INSERT INTO mytable (person_id, "姓名", "打卡時間") '
                    "VALUES (?, ?, ?)",
                    (person["id"], person["name"], checked_at),
                )
                connection.commit()
            return jsonify(
                person_id=person["id"],
                name=person["name"],
                distance=round(float(distance), 4),
                checkin_time=checked_at,
                provider="local-lbph",
            ), 201
        except (OSError, ValueError, json.JSONDecodeError):
            app.logger.exception("Could not use local LBPH face model")
            return jsonify(error="本機人臉辨識模型無法讀取或執行。"), 500

    if _azure_face_configured():
        try:
            candidate = _azure_identify(image_bytes)
            if candidate is None:
                return jsonify(error="找不到相符的人員。"), 404
            person_id = candidate.get("personId")
            confidence = candidate.get("confidence")
            checked_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
            with closing(_connect_db()) as connection:
                person = connection.execute(
                    "SELECT id, name FROM people WHERE azure_person_id = ?",
                    (person_id,),
                ).fetchone()
                if person is None:
                    return jsonify(error="辨識結果不在目前人員名冊中。"), 404
                connection.execute(
                    'INSERT INTO mytable (person_id, "姓名", "打卡時間") '
                    "VALUES (?, ?, ?)",
                    (person["id"], person["name"], checked_at),
                )
                connection.commit()
            return jsonify(
                person_id=person["id"],
                name=person["name"],
                similarity=round(confidence, 4) if isinstance(confidence, (int, float)) else None,
                checkin_time=checked_at,
                provider="azure-face",
            ), 201
        except requests.RequestException:
            app.logger.exception("Azure Face recognition request failed")
            return jsonify(error="無法連線到 Azure Face，請稍後再試。"), 502
        except (RuntimeError, ValueError) as exc:
            status = 400 if isinstance(exc, ValueError) else 502
            return jsonify(error=str(exc)), status
        except sqlite3.Error:
            app.logger.exception("Could not recognize with Azure Face or save check-in")
            return jsonify(error="人臉辨識或打卡資料儲存失敗，請稍後再試。"), 500

    embedding, error, status = _request_face_embedding(image_base64)
    if error:
        return jsonify(error=error), status
    try:
        threshold = float(os.getenv("FACE_RECOGNITION_THRESHOLD", "0.65"))
    except ValueError:
        return jsonify(error="FACE_RECOGNITION_THRESHOLD 設定無效。"), 500
    if not 0 < threshold <= 1:
        return jsonify(error="FACE_RECOGNITION_THRESHOLD 必須介於 0 與 1。"), 500

    try:
        with closing(_connect_db()) as connection:
            candidates = connection.execute(
                "SELECT id, name, face_embedding FROM people "
                "WHERE face_embedding IS NOT NULL"
            ).fetchall()
            matches = []
            for candidate in candidates:
                try:
                    stored_embedding = json.loads(candidate["face_embedding"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    app.logger.warning("Ignoring invalid face embedding for person %s", candidate["id"])
                    continue
                if not isinstance(stored_embedding, list):
                    continue
                similarity = _cosine_similarity(embedding, stored_embedding)
                if similarity is not None:
                    matches.append((similarity, candidate))
            if not matches:
                return jsonify(error="尚未建立可用的人臉模型。"), 404
            similarity, person = max(matches, key=lambda match: match[0])
            if similarity < threshold:
                return jsonify(error="找不到相符的人員。", similarity=round(similarity, 4)), 404
            checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
            connection.execute(
                'INSERT INTO mytable (person_id, "姓名", "打卡時間") VALUES (?, ?, ?)',
                (person["id"], person["name"], checked_at),
            )
            connection.commit()
    except sqlite3.Error:
        app.logger.exception("Could not recognize face or save SQLite check-in")
        return jsonify(error="人臉辨識或打卡資料儲存失敗，請稍後再試。"), 500
    return jsonify(
        person_id=person["id"],
        name=person["name"],
        similarity=round(similarity, 4),
        checkin_time=checked_at,
    ), 201


@app.errorhandler(413)
def request_too_large(_):
    return jsonify(error="請求大小超過限制。"), 413


@app.get("/healthz")
def healthz():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(debug=True)
