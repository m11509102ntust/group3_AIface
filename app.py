import base64
import binascii
import math
import os
import threading
import time
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024

FACE_DETECT_URL = "https://api-face-detect.knowlez.com/v1/face/detect"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FACE_DETECT_RATE_LIMIT = 10
FACE_DETECT_RATE_WINDOW = 60
_face_detect_requests = defaultdict(deque)
_face_detect_rate_lock = threading.Lock()


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


def bmi_guidance(bmi):
    if bmi < 18.5:
        return {
            "label": "體重過輕",
            "tone": "low",
            "advice": "留意是否攝取足夠且均衡的營養，規律進食並搭配適度肌力活動；若體重持續下降，建議諮詢醫師或營養師。",
        }
    if bmi < 24:
        return {
            "label": "健康體位",
            "tone": "healthy",
            "advice": "維持均衡飲食與規律運動，並持續留意睡眠及生活習慣，幫助保持健康體位。",
        }
    if bmi < 27:
        return {
            "label": "體重過重",
            "tone": "high",
            "advice": "可從減少含糖飲料、增加蔬菜與日常活動量開始，循序建立適合自己的健康習慣。",
        }
    return {
        "label": "肥胖",
        "tone": "high",
        "advice": "建議與醫師或營養師討論適合自己的飲食及運動計畫，並留意血壓、血糖等健康指標。",
    }


def parse_positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


@app.route("/", methods=["GET", "POST"])
def index():
    height_input = ""
    weight_input = ""
    errors = {}
    result = None

    if request.method == "POST":
        height_input = request.form.get("height", "").strip()
        weight_input = request.form.get("weight", "").strip()
        height = parse_positive_number(height_input)
        weight = parse_positive_number(weight_input)

        if height is None:
            errors["height"] = "請輸入大於 0 的有效身高（公分）。"
        if weight is None:
            errors["weight"] = "請輸入大於 0 的有效體重（公斤）。"

        if not errors:
            height_meters = height / 100
            height_squared = height_meters * height_meters
            if not math.isfinite(height_squared) or height_squared == 0:
                errors["height"] = "無法使用這組數值計算 BMI，請檢查身高與體重。"
            else:
                bmi = weight / height_squared
                if not math.isfinite(bmi) or bmi == 0:
                    errors["weight"] = "無法使用這組數值計算 BMI，請檢查身高與體重。"
                else:
                    displayed_bmi = round(bmi, 1)
                    result = {"bmi": f"{displayed_bmi:.1f}", **bmi_guidance(displayed_bmi)}

    return render_template(
        "index.html",
        height_input=height_input,
        weight_input=weight_input,
        errors=errors,
        result=result,
        face_detection_configured=bool(os.getenv("FACE_DETECT_API_KEY", "").strip()),
    )


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
