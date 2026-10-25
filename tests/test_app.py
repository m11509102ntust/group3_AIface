import base64
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests

from app import (
    PIN_LOGIN_MAX_ATTEMPTS,
    _face_detect_rate_lock,
    _face_detect_requests,
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
        with _face_detect_rate_lock:
            _face_detect_requests.clear()
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
        response = self.client.post(
            "/api/people",
            json={"name": name, "pin": pin},
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        if response.status_code == 409 and response.get_json()["needs_confirmation"]:
            response = self.client.post(
                "/api/people",
                json={"name": name, "pin": pin, "confirm_new_person": True},
                headers={"X-CSRF-Token": self._csrf_token()},
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

    def test_initial_page_renders_face_recognition_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("FaceSense", page)
        self.assertIn("AI 人臉偵測系統", page)
        self.assertIn("即時人臉偵測", page)
        self.assertIn("個人 PIN", page)
        self.assertIn("管理員登入（人員管理）", page)
        self.assertIn("登入後即可查看已儲存的打卡紀錄", page)
        self.assertIn("不辨識身份或記錄打卡", page)

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

    def test_admin_confirms_new_person_then_individual_pin_records_checkin(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()

        response = self.client.post(
            "/api/people",
            json={"name": " 王小明 ", "pin": "123456"},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["needs_confirmation"])

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mytable").fetchone()[0], 0)

        response = self.client.post(
            "/api/people",
            json={
                "name": "王小明",
                "pin": "123456",
                "confirm_new_person": True,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
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
        finally:
            app.secret_key = original_secret_key

    def test_root_route_rejects_post_requests(self):
        response = self.client.post("/", data={"unused": "data"})
        self.assertEqual(response.status_code, 405)

    def test_face_detection_requires_server_side_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(
                "/api/face/detect",
                json={"image_base64": "aGVsbG8="},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("API 金鑰", response.get_json()["error"])

    @patch.dict(os.environ, {"FACE_DETECT_API_KEY": "server-only-test-key"})
    @patch("app.requests.post")
    def test_face_detection_forwards_image_using_server_key(self, mock_post):
        image_bytes = b"\xff\xd8\xffsample-jpeg"
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        provider_response = Mock()
        provider_response.status_code = 201
        provider_response.json.return_value = {"faces": []}
        mock_post.return_value = provider_response

        response = self.client.post(
            "/api/face/detect",
            json={"image_base64": image_base64},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"faces": []})
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.kwargs["headers"],
            {"x-api-key": "server-only-test-key"},
        )
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {"image_base64": image_base64},
        )
        self.assertNotIn("server-only-test-key", response.get_data(as_text=True))

    @patch.dict(os.environ, {"FACE_DETECT_API_KEY": "server-only-test-key"})
    @patch("app.requests.post")
    def test_face_detection_rejects_invalid_image_data(self, mock_post):
        response = self.client.post(
            "/api/face/detect",
            json={"image_base64": base64.b64encode(b"not an image").decode("ascii")},
        )

        self.assertEqual(response.status_code, 400)
        mock_post.assert_not_called()

    @patch.dict(os.environ, {"FACE_DETECT_API_KEY": "server-only-test-key"})
    @patch("app.requests.post", side_effect=requests.Timeout)
    def test_face_detection_reports_upstream_timeout(self, _mock_post):
        image_base64 = base64.b64encode(b"\xff\xd8\xffsample-jpeg").decode("ascii")

        response = self.client.post(
            "/api/face/detect",
            json={"image_base64": image_base64},
        )

        self.assertEqual(response.status_code, 504)
        self.assertIn("逾時", response.get_json()["error"])

    def test_face_detection_rate_limits_requests(self):
        with patch.dict(os.environ, {}, clear=True):
            responses = [
                self.client.post(
                    "/api/face/detect",
                    json={"image_base64": "aGVsbG8="},
                )
                for _ in range(11)
            ]

        self.assertEqual(responses[-1].status_code, 429)


if __name__ == "__main__":
    unittest.main()
