const cameraFrame = document.getElementById("camera-frame");
const video = document.getElementById("camera-video");
const canvas = document.getElementById("detection-canvas");
const startButton = document.getElementById("start-camera");
const detectButton = document.getElementById("detect-face");
const stopButton = document.getElementById("stop-camera");
const cameraStatus = document.getElementById("camera-status");
const detectionResult = document.getElementById("detection-result");

let cameraStream = null;

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
    const response = await fetch("/api/face/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_base64: imageBase64 }),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "人臉偵測失敗，請稍後重試。");
    }
    detectionResult.textContent = JSON.stringify(result, null, 2);
    cameraStatus.textContent = "偵測完成。此服務只偵測人臉，不會辨識姓名或進行打卡。";
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
