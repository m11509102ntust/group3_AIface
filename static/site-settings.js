const faceDetectionToggle = document.getElementById("face-detection-enabled");
const settingsStatus = document.getElementById("settings-status");

if (faceDetectionToggle) {
  faceDetectionToggle.addEventListener("change", async () => {
    const enabled = faceDetectionToggle.checked;
    const stateLabel = faceDetectionToggle.nextElementSibling;
    faceDetectionToggle.disabled = true;
    settingsStatus.textContent = "正在儲存設定…";

    try {
      const response = await fetch("/api/settings/face-detection", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": faceDetectionToggle.dataset.csrfToken,
        },
        body: JSON.stringify({ enabled }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "設定儲存失敗，請稍後重試。");
      }

      stateLabel.textContent = result.enabled ? "啟用中" : "已停用";
      settingsStatus.textContent = result.enabled
        ? "人臉偵測已啟用。"
        : "人臉偵測已停用。";
    } catch (error) {
      faceDetectionToggle.checked = !enabled;
      settingsStatus.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    } finally {
      faceDetectionToggle.disabled = false;
    }
  });
}
