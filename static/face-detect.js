const cameraFrame = document.getElementById("camera-frame");
const video = document.getElementById("camera-video");
const canvas = document.getElementById("detection-canvas");
const startButton = document.getElementById("start-camera");
const detectButton = document.getElementById("detect-face");
const stopButton = document.getElementById("stop-camera");
const cameraStatus = document.getElementById("camera-status");
const detectionResult = document.getElementById("detection-result");

let cameraStream = null;
let fallbackEnrollmentImageBase64 = null;

async function countdownCapture(seconds = 5) {
  if (!cameraStream || !video.videoWidth || !video.videoHeight) {
    cameraStatus.textContent = "攝影機畫面尚未就緒，無法拍照建模。";
    return;
  }

  const maxDimension = 1024;
  for (let remaining = seconds; remaining > 0; remaining -= 1) {
    cameraStatus.textContent = `辨識失敗，倒數拍照建模：${remaining} 秒`;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }

  const scale = Math.min(1, maxDimension / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  fallbackEnrollmentImageBase64 =
    canvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1];
  try {
    const response = await fetch("/api/face/enrollment-requests", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": cameraFrame.dataset.csrfToken,
      },
      body: JSON.stringify({ image_base64: fallbackEnrollmentImageBase64 }),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "待審照片提交失敗。");
    }
    fallbackEnrollmentImageBase64 = null;
    cameraStatus.textContent =
      "照片已提交管理員審核；核准並建立人員後才會啟用下次辨識。";
  } catch (error) {
    cameraStatus.textContent =
      error instanceof TypeError
        ? "無法連線至網站，請檢查網路後重試。"
        : error.message;
  }
}

if (!cameraFrame) {
  // This script is shared by pages that do not contain a camera.
} else {
function stopCamera() {
  if (cameraStream) {
    cameraStream.getTracks().forEach((track) => track.stop());
    cameraStream = null;
  }
  video.srcObject = null;
  cameraFrame.classList.remove("camera-active");
  startButton.disabled = false;
  detectButton.disabled = true;
  stopButton.disabled = true;
}

startButton.addEventListener("click", async () => {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    cameraStatus.textContent = "此瀏覽器不支援攝影機，請使用最新版瀏覽器並透過 HTTPS 開啟網站。";
    return;
  }

  startButton.disabled = true;
  cameraStatus.textContent = "正在等待攝影機權限…";
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: "user" },
    });
    video.srcObject = cameraStream;
    await video.play();
    cameraFrame.classList.add("camera-active");
    detectButton.disabled = false;
    stopButton.disabled = false;
    cameraStatus.textContent = "攝影機已啟動。偵測時只會送出當下拍攝的單張影像。";
  } catch (error) {
    stopCamera();
    cameraStatus.textContent =
      error.name === "NotAllowedError"
        ? "攝影機權限遭拒，請在瀏覽器設定中允許使用攝影機。"
        : "無法啟動攝影機，請確認設備、權限及 HTTPS 連線。";
  }
});

stopButton.addEventListener("click", () => {
  stopCamera();
  cameraStatus.textContent = "攝影機已停止。";
});

detectButton.addEventListener("click", async () => {
  if (!cameraStream || !video.videoWidth || !video.videoHeight) {
    cameraStatus.textContent = "攝影機畫面尚未就緒，請稍後再試。";
    return;
  }

  const maxDimension = 1024;
  const scale = Math.min(1, maxDimension / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  const imageBase64 = canvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1];

  detectButton.disabled = true;
  cameraStatus.textContent = "正在分析影像…";
  detectionResult.textContent = "等待人臉偵測服務回應…";

  try {
    const mode = cameraFrame.dataset.faceMode || "enroll";
    const headers = { "Content-Type": "application/json" };
    const csrfToken =
      cameraFrame.dataset.csrfToken ||
      document.getElementById("person-form")?.dataset.csrfToken;
    if (csrfToken) {
      headers["X-CSRF-Token"] = csrfToken;
    }
    const body = { image_base64: imageBase64 };
    let endpoint = "/api/face/detect";
    if (mode === "enroll") {
      endpoint = "/api/face/enroll";
      body.person_id = Number(
        document.getElementById("enroll-person-id")?.value,
      );
    } else if (mode === "recognize") {
      endpoint = "/api/face/recognize";
    }
    const response = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok && mode === "recognize" && response.status === 404) {
      detectionResult.textContent = result.error || "找不到相符的人員。";
      await countdownCapture(5);
      return;
    }
    if (!response.ok) {
      throw new Error(result.error || "人臉偵測失敗，請稍後重試。");
    }
    detectionResult.textContent = JSON.stringify(result, null, 2);
    cameraStatus.textContent =
      mode === "recognize"
        ? `${result.name} 已完成臉部辨識並打卡。`
        : mode === "enroll"
          ? `${result.name} 的人臉模型已建立。`
          : "偵測完成。";
  } catch (error) {
    cameraStatus.textContent =
      error instanceof TypeError
        ? "無法連線至網站，請檢查網路後重試。"
        : error.message;
    detectionResult.textContent = "目前沒有可顯示的偵測結果。";
  } finally {
    detectButton.disabled = !cameraStream;
  }
});

window.addEventListener("pagehide", stopCamera);
}
