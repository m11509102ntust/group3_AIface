const checkinForm = document.getElementById("checkin-form");
const checkinStatus = document.getElementById("checkin-status");
const personForm = document.getElementById("person-form");
const personStatus = document.getElementById("person-status");
const enrollmentVideo = document.getElementById("enrollment-video");
const enrollmentPhotoPreview = document.getElementById("enrollment-photo-preview");
const enrollmentCanvas = document.getElementById("enrollment-canvas");
const enrollmentFrame = document.getElementById("enrollment-camera-frame");
const enrollmentStatus = document.getElementById("enrollment-status");
const enrollmentCountdown = document.getElementById("enrollment-countdown");
const enrollmentSubmitHint = document.getElementById("enrollment-submit-hint");
const startEnrollmentCamera = document.getElementById("start-enrollment-camera");
const captureEnrollmentPhoto = document.getElementById("capture-enrollment-photo");
const stopEnrollmentCamera = document.getElementById("stop-enrollment-camera");
const enrollmentEnabled = personForm?.dataset.enabled === "true";

let enrollmentStream = null;
let capturedFaceImageBase64 = null;

function displayLocalTimes() {
  document.querySelectorAll("[data-utc-time]").forEach((element) => {
    const date = new Date(element.dataset.utcTime);
    if (!Number.isNaN(date.getTime())) {
      element.textContent = date.toLocaleString();
    }
  });
}

function addCheckinRow(result) {
  const records = document.getElementById("checkin-records");
  if (!records) {
    return;
  }

  document.getElementById("checkin-empty")?.remove();
  const row = records.insertRow(0);
  row.insertCell().textContent = result.name;
  const timeCell = row.insertCell();
  const time = document.createElement("time");
  time.dataset.utcTime = result.checkin_time;
  time.textContent = result.checkin_time;
  timeCell.append(time);
  displayLocalTimes();
}

async function createCheckin(csrfToken) {
  return fetch("/api/checkins", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({}),
  });
}

function stopEnrollmentStream() {
  if (enrollmentStream) {
    enrollmentStream.getTracks().forEach((track) => track.stop());
    enrollmentStream = null;
  }
  if (enrollmentVideo) {
    enrollmentVideo.srcObject = null;
  }
  if (enrollmentCountdown) {
    enrollmentCountdown.textContent = "";
  }
  enrollmentFrame?.classList.remove("camera-active");
  if (startEnrollmentCamera) {
    startEnrollmentCamera.disabled = !enrollmentEnabled;
  }
  if (captureEnrollmentPhoto) {
    captureEnrollmentPhoto.disabled = true;
  }
  if (stopEnrollmentCamera) {
    stopEnrollmentCamera.disabled = true;
  }
}

if (startEnrollmentCamera) {
  startEnrollmentCamera.addEventListener("click", async () => {
    if (!enrollmentEnabled) {
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      enrollmentStatus.textContent =
        "此瀏覽器不支援攝影機，請使用最新版瀏覽器並透過 HTTPS 開啟網站。";
      return;
    }

    startEnrollmentCamera.disabled = true;
    capturedFaceImageBase64 = null;
    enrollmentPhotoPreview.hidden = true;
    enrollmentPhotoPreview.removeAttribute("src");
    enrollmentFrame.classList.remove("photo-captured");
    personForm.querySelector("button[type='submit']").disabled = true;
    updateEnrollmentSubmitButton();
    enrollmentStatus.textContent = "正在等待攝影機權限…";
    try {
      enrollmentStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: "user" },
      });
      enrollmentVideo.srcObject = enrollmentStream;
      await enrollmentVideo.play();
      enrollmentFrame.classList.add("camera-active");
      stopEnrollmentCamera.disabled = false;
      captureEnrollmentPhoto.disabled = false;
      enrollmentStatus.textContent = "攝影機已啟動；按下拍照後將倒數 5 秒。";
    } catch (error) {
      stopEnrollmentStream();
      enrollmentStatus.textContent =
        error.name === "NotAllowedError"
          ? "攝影機權限遭拒，請在瀏覽器設定中允許使用攝影機。"
          : "無法啟動攝影機，請確認設備、權限及 HTTPS 連線。";
    }
  });
}

function updateEnrollmentSubmitButton() {
  const submitButton = personForm?.querySelector("button[type='submit']");
  if (!submitButton || !personForm) {
    return;
  }

  const missingRequirements = [];
  if (!enrollmentEnabled) {
    missingRequirements.push("管理員尚未完成打卡系統設定");
  } else {
    if (!capturedFaceImageBase64) {
      missingRequirements.push("啟動攝影機並完成倒數拍照");
    }
    if (!personForm.elements.namedItem("name").validity.valid) {
      missingRequirements.push("填寫人員姓名");
    }
    if (!personForm.elements.namedItem("pin").validity.valid) {
      missingRequirements.push("輸入 6 至 12 位數字 PIN");
    }
    if (!document.getElementById("face-image-consent").checked) {
      missingRequirements.push("勾選本人明確同意");
    }
  }

  submitButton.disabled = missingRequirements.length > 0;
  if (enrollmentSubmitHint) {
    enrollmentSubmitHint.textContent = missingRequirements.length
      ? `提交前還需：${missingRequirements.join("、")}。`
      : "資料已完成，現在可以提交管理員審核。";
  }
}

personForm?.addEventListener("input", updateEnrollmentSubmitButton);
personForm?.addEventListener("change", updateEnrollmentSubmitButton);
updateEnrollmentSubmitButton();

stopEnrollmentCamera?.addEventListener("click", () => {
  stopEnrollmentStream();
  enrollmentStatus.textContent = "攝影機已停止。";
});

async function captureFacePhoto() {
  if (!enrollmentStream || !enrollmentVideo.videoWidth || !enrollmentVideo.videoHeight) {
    throw new Error("攝影機畫面尚未就緒，請稍後再試。");
  }

  for (let seconds = 5; seconds > 0; seconds -= 1) {
    enrollmentCountdown.textContent = String(seconds);
    enrollmentStatus.textContent = `${seconds} 秒後拍照，請看向攝影機。`;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    if (!enrollmentStream) {
      throw new Error("攝影機已停止，請重新啟動後再試。");
    }
  }

  const maxDimension = 1024;
  const scale = Math.min(
    1,
    maxDimension / Math.max(enrollmentVideo.videoWidth, enrollmentVideo.videoHeight),
  );
  enrollmentCanvas.width = Math.round(enrollmentVideo.videoWidth * scale);
  enrollmentCanvas.height = Math.round(enrollmentVideo.videoHeight * scale);
  const context = enrollmentCanvas.getContext("2d");
  if (!context) {
    throw new Error("無法建立拍照畫面，請重新載入後再試。");
  }
  context.drawImage(
    enrollmentVideo,
    0,
    0,
    enrollmentCanvas.width,
    enrollmentCanvas.height,
  );
  return enrollmentCanvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1];
}

captureEnrollmentPhoto?.addEventListener("click", async () => {
  captureEnrollmentPhoto.disabled = true;
  startEnrollmentCamera.disabled = true;
  try {
    capturedFaceImageBase64 = await captureFacePhoto();
    enrollmentPhotoPreview.src = `data:image/jpeg;base64,${capturedFaceImageBase64}`;
    enrollmentPhotoPreview.hidden = false;
    enrollmentFrame.classList.add("photo-captured");
    stopEnrollmentStream();
    enrollmentStatus.textContent = "照片已拍攝並可預覽；確認後填寫資料提交。";
    updateEnrollmentSubmitButton();
  } catch (error) {
    enrollmentCountdown.textContent = "";
    enrollmentStatus.textContent = error.message;
    if (enrollmentStream) {
      captureEnrollmentPhoto.disabled = false;
      stopEnrollmentCamera.disabled = false;
      startEnrollmentCamera.disabled = true;
    }
  }
});

async function submitEnrollmentRequest(name, pin, faceImageBase64) {
  return fetch("/api/enrollment-requests", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": personForm.dataset.csrfToken,
    },
    body: JSON.stringify({
      name,
      pin,
      face_image_base64: faceImageBase64,
      face_image_consent:
        document.getElementById("face-image-consent")?.checked === true,
    }),
  });
}

if (checkinForm) {
  checkinForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = checkinForm.querySelector("button[type='submit']");
    submitButton.disabled = true;
    checkinStatus.textContent = "正在儲存打卡資料…";

    try {
      const response = await createCheckin(checkinForm.dataset.csrfToken);
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "打卡失敗，請稍後重試。");
      }
      addCheckinRow(result);
      checkinStatus.textContent = `${result.name} 已完成打卡。`;
    } catch (error) {
      checkinStatus.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    } finally {
      submitButton.disabled = false;
    }
  });
}

if (personForm) {
  personForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const nameInput = personForm.elements.namedItem("name");
    const pinInput = personForm.elements.namedItem("pin");
    const submitButton = personForm.querySelector("button[type='submit']");
    const name = nameInput.value.trim();
    const pin = pinInput.value;
    if (!capturedFaceImageBase64) {
      personStatus.textContent = "請先完成拍照，再提交建檔申請。";
      return;
    }
    submitButton.disabled = true;
    personStatus.textContent = "正在提交待審建檔申請…";

    try {
      const response = await submitEnrollmentRequest(
        name,
        pin,
        capturedFaceImageBase64,
      );
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "建檔申請提交失敗，請稍後重試。");
      }

      personForm.reset();
      capturedFaceImageBase64 = null;
      enrollmentPhotoPreview.hidden = true;
      enrollmentPhotoPreview.removeAttribute("src");
      enrollmentFrame.classList.remove("photo-captured");
      enrollmentCountdown.textContent = "";
      enrollmentStatus.textContent = "申請已提交；待管理員審核後才會正式建檔。";
      personStatus.textContent = `${result.name} 的建檔申請已送出，核准前不能登入打卡。`;
    } catch (error) {
      personStatus.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    } finally {
      updateEnrollmentSubmitButton();
    }
  });
}

document.getElementById("pending-enrollment-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest(".approve-enrollment, .reject-enrollment");
  if (!button || button.disabled) {
    return;
  }
  const requestItem = button.closest("li");
  const requestId = encodeURIComponent(requestItem.dataset.requestId);
  const approve = button.classList.contains("approve-enrollment");
  if (
    !window.confirm(
      approve
        ? `確定核准「${requestItem.querySelector("strong").textContent}」的建檔申請？`
        : `確定拒絕並刪除「${requestItem.querySelector("strong").textContent}」的待審資料？`,
    )
  ) {
    return;
  }

  const reviewStatus = document.getElementById("enrollment-review-status");
  button.disabled = true;
  reviewStatus.textContent = approve ? "正在核准建檔…" : "正在刪除待審資料…";
  try {
    const response = await fetch(
      approve
        ? `/api/enrollment-requests/${requestId}/approve`
        : `/api/enrollment-requests/${requestId}`,
      {
        method: approve ? "POST" : "DELETE",
        headers: { "X-CSRF-Token": event.currentTarget.dataset.csrfToken },
      },
    );
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "審核操作失敗，請稍後重試。");
    }
    window.location.reload();
  } catch (error) {
    button.disabled = false;
    reviewStatus.textContent =
      error instanceof TypeError
        ? "無法連線至網站，請檢查網路後重試。"
        : error.message;
  }
});

document.getElementById("member-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-face-photo]");
  if (!button || button.disabled) {
    return;
  }
  const memberItem = button.closest("li");
  if (!window.confirm(`確定刪除「${memberItem.dataset.name}」的人臉照片？`)) {
    return;
  }

  button.disabled = true;
  try {
    const response = await fetch(
      `/api/people/${encodeURIComponent(memberItem.dataset.personId)}/face-photo`,
      {
        method: "DELETE",
        headers: { "X-CSRF-Token": personForm.dataset.csrfToken },
      },
    );
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "照片刪除失敗，請稍後重試。");
    }
    memberItem.dataset.hasFaceSample = "false";
    memberItem.querySelector(".face-sample-state").textContent = "尚無照片";
    button.disabled = true;
    personStatus.textContent = `${memberItem.dataset.name} 的照片已刪除。`;
  } catch (error) {
    button.disabled = false;
    personStatus.textContent =
      error instanceof TypeError
        ? "無法連線至網站，請檢查網路後重試。"
        : error.message;
  }
});

window.addEventListener("pagehide", stopEnrollmentStream);
displayLocalTimes();
