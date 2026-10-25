import base64
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests

from app import app, _face_detect_rate_lock, _face_detect_requests


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

    def test_initial_page_renders_face_recognition_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("FaceSense", page)
        self.assertIn("AI 人臉偵測系統", page)
        self.assertIn("即時人臉偵測", page)
        self.assertIn("管理員密碼", page)
        self.assertIn("登入後即可查看已儲存的打卡紀錄", page)
        self.assertIn("不辨識身份或記錄打卡", page)

    def test_checkins_require_login(self):
        response = self.client.post(
            "/api/checkins",
            json={"name": "王小明"},
        )
        self.assertEqual(response.status_code, 401)

    def test_login_rejects_wrong_password_and_missing_csrf(self):
        response = self.client.post("/login", data={"password": "wrong"})
        self.assertEqual(response.status_code, 400)

        response = self._login(password="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertIn("密碼不正確", response.get_data(as_text=True))

        response = self.client.post(
            "/api/checkins",
            json={"name": "王小明"},
        )
        self.assertEqual(response.status_code, 401)

    def test_unknown_name_requires_confirmation_then_saves_person_and_checkin(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()

        response = self.client.post(
            "/api/checkins",
            json={"name": " 王小明 "},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["needs_confirmation"])

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mytable").fetchone()[0], 0)

        response = self.client.post(
            "/api/checkins",
            json={"name": "王小明", "confirm_new_person": True},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "王小明")
        self.assertTrue(response.get_json()["checkin_time"].endswith("Z"))

        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1)
            row = connection.execute(
                'SELECT "姓名", "打卡時間" FROM mytable'
            ).fetchone()
        self.assertEqual(row[0], "王小明")
        self.assertTrue(row[1].endswith("Z"))

    def test_existing_name_is_reused_case_insensitively_and_each_checkin_is_saved(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()
        headers = {"X-CSRF-Token": csrf_token}
        self.client.post(
            "/api/checkins",
            json={"name": "Alex", "confirm_new_person": True},
            headers=headers,
        )
        response = self.client.post(
            "/api/checkins",
            json={"name": " alex "},
            headers=headers,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "Alex")
        with closing(sqlite3.connect(os.environ["CHECKIN_DB_PATH"])) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mytable").fetchone()[0], 2)

    def test_checkin_rejects_invalid_names_and_missing_csrf(self):
        self.assertEqual(self._login().status_code, 302)
        response = self.client.post(
            "/api/checkins",
            json={"name": "  "},
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/checkins", json={"name": "王小明"})
        self.assertEqual(response.status_code, 400)

    def test_logout_clears_authenticated_session(self):
        self.assertEqual(self._login().status_code, 302)
        csrf_token = self._csrf_token()
        response = self.client.post("/logout", data={"csrf_token": csrf_token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.post("/api/checkins", json={"name": "王小明"}).status_code,
            401,
        )

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
