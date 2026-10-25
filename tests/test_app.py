import unittest

from app import app, bmi_guidance


class FaceSenseAppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_initial_page_renders_face_recognition_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("FaceSense", page)
        self.assertIn("AI 人臉辨識系統", page)
        self.assertIn("即時辨識畫面", page)
        self.assertIn("最近辨識紀錄", page)

    def test_post_route_still_renders_dashboard(self):
        response = self.client.post("/", data={"height": "170", "weight": "65"})
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("FaceSense", page)
        self.assertIn("系統運作中", page)

    def test_taiwan_adult_thresholds_are_preserved(self):
        for bmi, category in [
            (18.4, "體重過輕"),
            (18.5, "健康體位"),
            (23.9, "健康體位"),
            (24.0, "體重過重"),
            (26.9, "體重過重"),
            (27.0, "肥胖"),
        ]:
            with self.subTest(bmi=bmi):
                self.assertEqual(bmi_guidance(bmi)["label"], category)

    def test_invalid_post_data_does_not_break_dashboard(self):
        for height, weight in [
            ("", "65"),
            ("0", "65"),
            ("abc", "65"),
            ("170", "-2"),
            ("inf", "65"),
            ("170", "nan"),
        ]:
            with self.subTest(height=height, weight=weight):
                response = self.client.post(
                    "/",
                    data={"height": height, "weight": weight},
                )
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("FaceSense", page)
                self.assertNotIn("YOUR RESULT", page)

    def test_input_is_not_reflected_into_dashboard(self):
        response = self.client.post(
            "/",
            data={"height": "<script>alert(1)</script>", "weight": "65"},
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>", page)
        self.assertIn("AI 人臉辨識系統", page)


if __name__ == "__main__":
    unittest.main()
