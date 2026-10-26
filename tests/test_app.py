import base64
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

from app import (
    PIN_LOGIN_MAX_ATTEMPTS,
    _pin_login_attempts,
    _pin_login_lock,
    app,
)


class FaceSenseAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_secret_key = app.secret_key
        self.original_testing = app.config["TESTING"]
        app.secret_key = "test-session-secret"
        app.config["TESTING"] = True
        self.environment = patch.dict(
            os.environ,
            {
                "CHECKIN_ADMIN_PASSWORD": "test-admin-password",
                "CHECKIN_DB_PATH": os.path.join(self.temp_dir.name, "checkins.sqlite3"),
            },
        )
        self.environment.start()
        self.client = app.test_client()
        with _pin_login_lock:
            _pin_login_attempts.clear()

    def tearDown(self):
        self.environment.stop()
        app.secret_key = self.original_secret_key
        app.config["TESTING"] = self.original_testing
        self.temp_dir.cleanup()

    def _csrf_token(self):
        response = self.client.get("/")
        match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def _login(self, password="test-admin-password"):
        return self.client.post(
            "/login",
            data={"csrf_token": self._csrf_token(), "password": password},
        )

    def _register_person(self, name="王小明", pin="123456"):
        csrf_token = self._csrf_token()
        response = self.client.post(
            "/api/enrollment-requests",
            json={
                "name": name,
                "pin": pin,
                "face_image_base64": base64.b64encode(b"\xff\xd8\xffsample").decode(
                    "ascii"
                ),
                "face_image_consent": True,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        if response.status_code == 202:
            response = self.client.post(
                f"/api/enrollment-requests/{response.get_json()['request_id']}/approve",
                headers={"X-CSRF-Token": csrf_token},
            )
        return response

    def _person_login(self, name="王小明", pin="123456"):
        return self.client.post(
            "/person-login",
            data={
                "csrf_token": self._csrf_token(),
                "name": name,
                "pin": pin,
            },
        )

    def test_initial_page_renders_face_enrollment_dashboard_without_detection(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("FaceSense", page)
        self.assertIn("AI人臉辨識打卡系統測試", page)
        self.assertIn("倒數拍照建檔", page)
        self.assertIn("先拍照，再提交建檔", page)
        self.assertIn("提交管理員審核", page)
        self.assertNotIn("即時人臉偵測", page)
        self.assertNotIn("偵測人臉", page)
        self.assertNotIn("face-detect.js", page)
        self.assertIn("個人 PIN", page)
        self.assertIn("管理員登入（人員管理）", page)
        self.assertIn("登入後即可查看已儲存的打卡紀錄", page)
        self.assertIn("照片建檔不等於身份辨識", page)
        self.assertIn("今日打卡筆數", page)
        self.assertIn("系統設定與服務狀態", page)
        self.assertNotIn("示意", page)
        self.assertNotIn("1,284", page)
        self.assertNotIn("待確認事件", page)
        self.assertNotIn('aria-label="通知"', page)

    def test_enrollment_request_requires_admin_approval_before_person_is_created(self):
        csrf_token = self._csrf_token()
        image_bytes = b"\xff\xd8\xffsample-jpeg"
        response = self.client.post(
            "/api/enrollment-requests",
            json={
                "name": " 王小明 ",
                "pin": "123456",
                "face_image_base64": base64.b64encode(image_bytes).decode("ascii"),
                "face_image_consent": True,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 202)
        request_id = response.get_json()["request_id"]
        self.assertEqual(response.get_json()["status"], "pending")
        self.assertEqual(
            self.client.get(
                f"/api/enrollment-requests/{request_id}/photo"
            ).status_code,
            401,
        )

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0
            )
            pending = connection.execute(
                "SELECT name, pin_hash, image, consented_at "
                "FROM enrollment_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        self.assertEqual(pending[0], "王小明")
        self.assertNotEqual(pending[1], "123456")
        self.assertEqual(pending[2], image_bytes)
        self.assertTrue(pending[3])
        login = self._person_login("王小明", "123456")
        self.assertIn("person_login_error=1", login.location)

        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()
        response = self.client.post(
            f"/api/enrollment-requests/{request_id}/approve",
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "王小明")

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM enrollment_requests").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT image FROM face_samples").fetchone()[0],
                image_bytes,
            )

        self.client.post("/logout", data={"csrf_token": csrf_token})
        self.assertEqual(self._person_login("王小明", "123456").status_code, 302)

    def test_rejected_enrollment_request_is_deleted_and_never_activated(self):
        response = self.client.post(
            "/api/enrollment-requests",
            json={
                "name": "王小明",
                "pin": "123456",
                "face_image_base64": base64.b64encode(b"\xff\xd8\xffsample").decode(
                    "ascii"
                ),
                "face_image_consent": True,
            },
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 202)
        request_id = response.get_json()["request_id"]

        self.assertEqual(self._login().status_code, 302)
        response = self.client.delete(
            f"/api/enrollment-requests/{request_id}",
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deleted"])
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM enrollment_requests"
                ).fetchone()[0],
                0,
            )
        self.assertIn(
            "person_login_error=1",
            self._person_login("王小明", "123456").location,
        )

    def test_enrollment_requests_validate_consent_photo_and_duplicate_name(self):
        headers = {"X-CSRF-Token": self._csrf_token()}
        payload = {
            "name": "王小明",
            "pin": "123456",
            "face_image_base64": base64.b64encode(b"not an image").decode("ascii"),
            "face_image_consent": True,
        }
        self.assertEqual(
            self.client.post(
                "/api/enrollment-requests", json=payload, headers=headers
            ).status_code,
            400,
        )
        payload["face_image_base64"] = base64.b64encode(b"\xff\xd8\xffsample").decode(
            "ascii"
        )
        payload["face_image_consent"] = False
        self.assertEqual(
            self.client.post(
                "/api/enrollment-requests", json=payload, headers=headers
            ).status_code,
            400,
        )
        payload["face_image_consent"] = True
        self.assertEqual(
            self.client.post(
                "/api/enrollment-requests", json=payload, headers=headers
            ).status_code,
            202,
        )
        self.assertEqual(
            self.client.post(
                "/api/enrollment-requests",
                json=payload,
                headers=headers,
            ).status_code,
            409,
        )

    def test_admin_cannot_bypass_approval_to_create_new_people(self):
        self.assertEqual(self._login().status_code, 302)
        response = self.client.post(
            "/api/people",
            json={
                "name": "王小明",
                "pin": "123456",
                "confirm_new_person": True,
            },
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("先提交建檔申請", response.get_json()["error"])
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0
            )

    def test_dashboard_metrics_use_real_attendance_and_are_private(self):
        self.assertEqual(self._login().status_code, 302)
        self.assertEqual(self._register_person("王小明", "123456").status_code, 201)
        today = datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            connection.execute(
                'INSERT INTO mytable (person_id, "姓名", "打卡時間") '
                "VALUES (1, ?, ?), (1, ?, ?)",
                (
                    "王小明",
                    f"{today}T08:00:00Z",
                    "王小明",
                    f"{today}T09:00:00Z",
                ),
            )
            connection.commit()

        admin_page = self.client.get("/").get_data(as_text=True)
        self.assertIn(">2</strong>", admin_page)
        self.assertIn(">1</strong>", admin_page)

        anonymous_client = app.test_client()
        anonymous_page = anonymous_client.get("/").get_data(as_text=True)
        self.assertNotIn(">2</strong>", anonymous_page)
        self.assertNotIn("王小明", anonymous_page)

    def test_checkins_require_login(self):
        response = self.client.post(
            "/api/checkins",
            json={},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self._login().status_code, 302)
        response = self.client.post(
            "/api/checkins",
            json={},
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_login_rejects_wrong_password_and_missing_csrf(self):
        response = self.client.post("/login", data={"password": "wrong"})
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            "/login",
            data={"csrf_token": self._csrf_token(), "password": "wrong"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("管理員密碼不正確", response.get_data(as_text=True))

        response = self.client.post(
            "/api/people",
            json={"name": "王小明", "pin": "123456"},
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_approves_person_then_individual_pin_records_checkin(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()

        response = self._register_person(" 王小明 ", "123456")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "王小明")

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1
            )
            stored_pin = connection.execute(
                "SELECT pin_salt, pin_hash FROM people"
            ).fetchone()
        self.assertNotEqual(stored_pin[0], "123456")
        self.assertNotEqual(stored_pin[1], "123456")

        self.assertEqual(self.client.post("/logout", data={"csrf_token": csrf_token}).status_code, 302)
        self.assertEqual(self._person_login().status_code, 302)
        response = self.client.post(
            "/api/checkins",
            json={"name": "冒名打卡"},
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "王小明")
        self.assertTrue(response.get_json()["checkin_time"].endswith("Z"))

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            row = connection.execute(
                'SELECT "姓名", "打卡時間" FROM mytable'
            ).fetchone()
        self.assertEqual(row[0], "王小明")
        self.assertTrue(row[1].endswith("Z"))

    def test_existing_name_requires_confirmation_before_pin_reset(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()
        headers = {"X-CSRF-Token": csrf_token}
        self.assertEqual(
            self._register_person("Alex", "123456").status_code,
            201,
        )
        response = self.client.post(
            "/api/people",
            json={"name": "alex", "pin": "654321"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["needs_reset_confirmation"])
        response = self.client.post(
            "/api/people",
            json={"name": "alex", "pin": "654321", "confirm_pin_reset": True},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["name"], "Alex")
        self.assertEqual(
            self.client.post("/logout", data={"csrf_token": csrf_token}).status_code,
            302,
        )
        failed_login = self._person_login("Alex", "123456")
        self.assertIn("person_login_error=1", failed_login.location)
        response = self._person_login("Alex", "654321")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")

    def test_pin_must_be_six_to_twelve_digits(self):
        self.assertEqual(self._login().status_code, 302)
        response = self.client.post(
            "/api/people",
            json={"name": "王小明", "pin": "12ab56"},
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/people",
            json={"name": "王小明", "pin": "12345"},
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 400)

    def test_logout_clears_authenticated_session(self):
        self._login()
        self._register_person()
        csrf_token = self._csrf_token()
        self.client.post("/logout", data={"csrf_token": csrf_token})
        self._person_login()
        response = self.client.post(
            "/logout",
            data={"csrf_token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post("/api/checkins", json={}).status_code, 401)

    def test_pin_login_limits_repeated_attempts(self):
        self._login()
        self._register_person()
        self.client.post("/logout", data={"csrf_token": self._csrf_token()})
        for _ in range(PIN_LOGIN_MAX_ATTEMPTS):
            response = self._person_login(pin="000000")
            self.assertEqual(response.status_code, 302)
        response = self._person_login(pin="000000")
        self.assertIn("person_login_error=2", response.location)

    def test_unregistered_person_cannot_self_register(self):
        response = self._person_login("未登記姓名", "123456")
        self.assertEqual(response.status_code, 302)
        self.assertIn("person_login_error=1", response.location)
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0],
                0,
            )

    def test_resetting_pin_invalidates_existing_employee_session(self):
        self._login()
        self.assertEqual(self._register_person("Alex", "123456").status_code, 201)

        employee_client = app.test_client()
        page = employee_client.get("/")
        csrf_match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            page.get_data(as_text=True),
        )
        self.assertIsNotNone(csrf_match)
        employee_login = employee_client.post(
            "/person-login",
            data={
                "csrf_token": csrf_match.group(1),
                "name": "Alex",
                "pin": "123456",
            },
        )
        self.assertEqual(employee_login.status_code, 302)

        admin_token = self._csrf_token()
        response = self.client.post(
            "/api/people",
            json={
                "name": "Alex",
                "pin": "654321",
                "confirm_pin_reset": True,
            },
            headers={"X-CSRF-Token": admin_token},
        )
        self.assertEqual(response.status_code, 200)
        employee_page = employee_client.get("/")
        self.assertIn("尚未登入", employee_page.get_data(as_text=True))
        response = employee_client.post(
            "/api/checkins",
            json={},
            headers={"X-CSRF-Token": csrf_match.group(1)},
        )
        self.assertEqual(response.status_code, 401)

    def test_existing_people_table_is_migrated_to_store_pin_hashes(self):
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            connection.execute(
                "CREATE TABLE people ("
                "id INTEGER PRIMARY KEY, name TEXT UNIQUE, created_at TEXT NOT NULL)"
            )
            connection.commit()

        self._login()
        self.client.get("/")
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(people)")
            }
        self.assertIn("pin_salt", columns)
        self.assertIn("pin_hash", columns)

    def test_checkin_authentication_is_disabled_until_server_secrets_are_set(self):
        original_secret_key = app.secret_key
        app.secret_key = None
        try:
            with patch.dict(os.environ, {"CHECKIN_ADMIN_PASSWORD": ""}):
                response = self.client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "CHECKIN_ADMIN_PASSWORD",
                    response.get_data(as_text=True),
                )
                response = self.client.post("/api/checkins", json={})
                self.assertEqual(response.status_code, 503)
                response = self.client.post("/api/enrollment-requests", json={})
                self.assertEqual(response.status_code, 503)
        finally:
            app.secret_key = original_secret_key

    def test_enrollment_warning_only_lists_missing_server_settings(self):
        with patch.dict(os.environ, {"CHECKIN_ADMIN_PASSWORD": ""}):
            page = self.client.get("/").get_data(as_text=True)
        self.assertIn("CHECKIN_ADMIN_PASSWORD", page)
        warning = page.split("管理員審核尚未啟用", 1)[1].split("</p>", 1)[0]
        self.assertNotIn("FLASK_SECRET_KEY", warning)

    def test_admin_can_save_and_delete_a_face_photo(self):
        self.assertEqual(
            self.client.delete("/api/people/1/face-photo").status_code,
            401,
        )
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()
        image_bytes = b"\xff\xd8\xffsample"

        response = self._register_person("王小明", "123456")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["has_face_sample"])
        person_id = response.get_json()["person_id"]

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            photo = connection.execute(
                "SELECT image, content_type, consented_at "
                "FROM face_samples WHERE person_id = ?",
                (person_id,),
            ).fetchone()
        self.assertEqual(photo[0], image_bytes)
        self.assertEqual(photo[1], "image/jpeg")
        self.assertTrue(photo[2])
        self.assertIn("照片已建檔", self.client.get("/").get_data(as_text=True))

        response = self.client.delete(f"/api/people/{person_id}/face-photo")
        self.assertEqual(response.status_code, 400)
        response = self.client.delete(
            f"/api/people/{person_id}/face-photo",
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deleted"])
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM face_samples").fetchone()[0],
                0,
            )

    def test_face_photo_requires_a_supported_image_and_explicit_consent(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()
        headers = {"X-CSRF-Token": csrf_token}
        response = self.client.post(
            "/api/people",
            json={
                "name": "王小明",
                "pin": "123456",
                "confirm_new_person": True,
                "face_image_base64": base64.b64encode(b"not an image").decode("ascii"),
                "face_image_consent": True,
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/people",
            json={
                "name": "王小明",
                "pin": "123456",
                "confirm_new_person": True,
                "face_image_base64": base64.b64encode(
                    b"\xff\xd8\xffsample-jpeg"
                ).decode("ascii"),
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0],
                0,
            )

    def test_root_route_rejects_post_requests(self):
        response = self.client.post("/", data={"unused": "data"})
        self.assertEqual(response.status_code, 405)

if __name__ == "__main__":
    unittest.main()
