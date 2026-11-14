const cameraFrame = document.getElementById("camera-frame");
const video = document.getElementById("camera-video");
const canvas = document.getElementById("detection-canvas");
const startButton = document.getElementById("start-camera");
const detectButton = document.getElementById("detect-face");
const saveFaceButton = document.getElementById("save-face");
const stopButton = document.getElementById("stop-camera");
const cameraStatus = document.getElementById("camera-status");
const detectionResult = document.getElementById("detection-result");

let cameraStream = null;
let capturedImageBase64 = null;

if (cameraFrame && video && canvas && startButton && detectButton && stopButton) {
  function stopCamera() {
    if (cameraStream) {
      cameraStream.getTracks().forEach((track) => track.stop());
      cameraStream = null;
    }
    video.srcObject = null;
    cameraFrame.classList.remove("camera-active");
    startButton.disabled = false;
    detectButton.disabled = true;
    if (saveFaceButton) {
      saveFaceButton.disabled = true;
    }
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
    capturedImageBase64 = canvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1];

    detectButton.disabled = true;
    detectionResult.textContent = "影像已擷取，儲存時會由 Azure Face 驗證必須只有一張人臉。";
    cameraStatus.textContent = "影像擷取完成；確認畫面只有一張人臉後，可儲存建檔。";
    if (saveFaceButton) {
      saveFaceButton.disabled = false;
    }
    detectButton.disabled = !cameraStream;
  });

  saveFaceButton?.addEventListener("click", async () => {
    const nameInput = document.getElementById("person-name");
    const personForm = document.getElementById("person-form");
    if (!capturedImageBase64 || !nameInput?.value.trim() || !personForm) {
      cameraStatus.textContent = "請先填寫人員姓名並完成單一人臉偵測。";
      return;
    }

    saveFaceButton.disabled = true;
    cameraStatus.textContent = "正在儲存人臉建檔…";
    try {
      const response = await fetch("/api/people/face", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": personForm.dataset.csrfToken,
        },
        body: JSON.stringify({
          name: nameInput.value.trim(),
          image_base64: capturedImageBase64,
        }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "人臉建檔失敗。");
      }
      detectionResult.textContent = `${result.name} 已完成 Azure Face 人臉建檔。`;
      cameraStatus.textContent = "人臉建檔成功，該人員可使用人臉辨識打卡。";
    } catch (error) {
      cameraStatus.textContent =
        error instanceof TypeError ? "網路連線失敗，請確認伺服器仍在執行。" : error.message;
      saveFaceButton.disabled = false;
    }
  });

  window.addEventListener("pagehide", stopCamera);
}
