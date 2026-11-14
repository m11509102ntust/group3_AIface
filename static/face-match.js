const faceCheckinButton = document.getElementById("face-checkin-button");
const faceCheckinPanel = document.getElementById("face-checkin-panel");
const faceCheckinFrame = document.getElementById("face-checkin-camera-frame");
const faceCheckinVideo = document.getElementById("face-checkin-video");
const faceCheckinCanvas = document.getElementById("face-checkin-canvas");
const startFaceCheckinButton = document.getElementById("start-face-checkin-camera");
const captureFaceCheckinButton = document.getElementById("capture-face-checkin");
const stopFaceCheckinButton = document.getElementById("stop-face-checkin-camera");
const faceCheckinStatus = document.getElementById("face-checkin-status");
const checkinForm = document.getElementById("checkin-form");

let faceCheckinStream = null;

if (
  faceCheckinButton &&
  faceCheckinPanel &&
  faceCheckinFrame &&
  faceCheckinVideo &&
  faceCheckinCanvas &&
  startFaceCheckinButton &&
  captureFaceCheckinButton &&
  stopFaceCheckinButton &&
  checkinForm
) {
  function stopFaceCheckinCamera() {
    if (faceCheckinStream) {
      faceCheckinStream.getTracks().forEach((track) => track.stop());
      faceCheckinStream = null;
    }
    faceCheckinVideo.srcObject = null;
    faceCheckinFrame.classList.remove("camera-active");
    startFaceCheckinButton.disabled = false;
    captureFaceCheckinButton.disabled = true;
    stopFaceCheckinButton.disabled = true;
  }

  faceCheckinButton.addEventListener("click", () => {
    faceCheckinPanel.hidden = false;
    faceCheckinStatus.textContent = "請按下「開啟相機」並允許瀏覽器使用相機。";
    faceCheckinButton.setAttribute("aria-expanded", "true");
  });

  startFaceCheckinButton.addEventListener("click", async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      faceCheckinStatus.textContent = "此瀏覽器不支援攝影機，請透過 HTTPS 或 localhost 開啟。";
      return;
    }

    startFaceCheckinButton.disabled = true;
    faceCheckinStatus.textContent = "正在等待攝影機權限…";
    try {
      faceCheckinStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: "user" },
      });
      faceCheckinVideo.srcObject = faceCheckinStream;
      await faceCheckinVideo.play();
      faceCheckinFrame.classList.add("camera-active");
      captureFaceCheckinButton.disabled = false;
      stopFaceCheckinButton.disabled = false;
      faceCheckinStatus.textContent = "相機已開啟，請將臉部置於畫面中央。";
    } catch (error) {
      stopFaceCheckinCamera();
      faceCheckinStatus.textContent =
        error.name === "NotAllowedError"
          ? "攝影機權限遭拒，請在瀏覽器設定中允許使用攝影機。"
          : "無法啟動攝影機，請確認設備與權限。";
    }
  });

  stopFaceCheckinButton.addEventListener("click", () => {
    stopFaceCheckinCamera();
    faceCheckinStatus.textContent = "相機已停止。";
  });

  captureFaceCheckinButton.addEventListener("click", async () => {
    if (!faceCheckinStream || !faceCheckinVideo.videoWidth) {
      faceCheckinStatus.textContent = "相機畫面尚未就緒，請稍後再試。";
      return;
    }

    const maxDimension = 1024;
    const scale = Math.min(
      1,
      maxDimension /
        Math.max(faceCheckinVideo.videoWidth, faceCheckinVideo.videoHeight),
    );
    faceCheckinCanvas.width = Math.round(faceCheckinVideo.videoWidth * scale);
    faceCheckinCanvas.height = Math.round(faceCheckinVideo.videoHeight * scale);
    faceCheckinCanvas
      .getContext("2d")
      .drawImage(
        faceCheckinVideo,
        0,
        0,
        faceCheckinCanvas.width,
        faceCheckinCanvas.height,
      );
    const imageBase64 = faceCheckinCanvas
      .toDataURL("image/jpeg", 0.85)
      .split(",", 2)[1];

    captureFaceCheckinButton.disabled = true;
    faceCheckinStatus.textContent = "正在進行 Azure Face 比對…";
    try {
      const response = await fetch("/api/face-checkins", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": checkinForm.dataset.csrfToken,
        },
        body: JSON.stringify({ image_base64: imageBase64 }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "人臉辨識打卡失敗。");
      }
      faceCheckinStatus.textContent = `${result.name} 人臉辨識打卡成功。`;
      faceCheckinButton.disabled = true;
    } catch (error) {
      faceCheckinStatus.textContent =
        error instanceof TypeError ? "網路連線失敗，請確認伺服器仍在執行。" : error.message;
      captureFaceCheckinButton.disabled = false;
    }
  });

  window.addEventListener("pagehide", stopFaceCheckinCamera);
}
