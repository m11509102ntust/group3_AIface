import base64
import os
import unittest
from unittest.mock import Mock, patch

import requests

from app import app, _face_detect_rate_lock, _face_detect_requests


class FaceSenseAppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with _face_detect_rate_lock:
            _face_detect_requests.clear()

    def test_initial_page_renders_face_recognition_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("FaceSense", page)
        self.assertIn("AI 人臉偵測系統", page)
        self.assertIn("即時人臉偵測", page)
        self.assertIn("示意辨識紀錄", page)
        self.assertIn("不辨識身份或記錄打卡", page)

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
