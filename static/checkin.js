const checkinForm = document.getElementById("checkin-form");
const checkinStatus = document.getElementById("checkin-status");
const personForm = document.getElementById("person-form");
const personStatus = document.getElementById("person-status");
const enrollmentVideo = document.getElementById("enrollment-video");
const enrollmentCanvas = document.getElementById("enrollment-canvas");
const enrollmentFrame = document.getElementById("enrollment-camera-frame");
const enrollmentStatus = document.getElementById("enrollment-status");
const enrollmentCountdown = document.getElementById("enrollment-countdown");
const startEnrollmentCamera = document.getElementById("start-enrollment-camera");
const stopEnrollmentCamera = document.getElementById("stop-enrollment-camera");

let enrollmentStream = null;

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
  enrollmentFrame?.classList.remove("camera-active");
  if (startEnrollmentCamera) {
    startEnrollmentCamera.disabled = false;
  }
  if (stopEnrollmentCamera) {
    stopEnrollmentCamera.disabled = true;
  }
}

if (startEnrollmentCamera) {
  startEnrollmentCamera.addEventListener("click", async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      enrollmentStatus.textContent =
        "此瀏覽器不支援攝影機，請使用最新版瀏覽器並透過 HTTPS 開啟網站。";
      return;
    }

    startEnrollmentCamera.disabled = true;
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
      enrollmentStatus.textContent = "攝影機已啟動，確認本人同意後即可拍照建檔。";
    } catch (error) {
      stopEnrollmentStream();
      enrollmentStatus.textContent =
        error.name === "NotAllowedError"
          ? "攝影機權限遭拒，請在瀏覽器設定中允許使用攝影機。"
          : "無法啟動攝影機，請確認設備、權限及 HTTPS 連線。";
    }
  });
}

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
  enrollmentCountdown.textContent = "拍照";
  return enrollmentCanvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1];
}

async function submitPerson(
  personForm,
  name,
  pin,
  confirmNewPerson,
  confirmPinReset,
  faceImageBase64,
) {
  return fetch("/api/people", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": personForm.dataset.csrfToken,
    },
    body: JSON.stringify({
      name,
      pin,
      confirm_new_person: confirmNewPerson,
      confirm_pin_reset: confirmPinReset,
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
    let confirmNewPerson = false;
    let confirmPinReset = false;
    submitButton.disabled = true;
    startEnrollmentCamera.disabled = true;
    personStatus.textContent = "正在確認人員資料…";

    try {
      let response = await submitPerson(
        personForm,
        name,
        pin,
        confirmNewPerson,
        confirmPinReset,
      );
      let result = await response.json();
      if (
        response.status === 409 &&
        (result.needs_confirmation || result.needs_reset_confirmation)
      ) {
        if (!window.confirm(result.message)) {
          personStatus.textContent = "已取消變更人員資料。";
          return;
        }
        confirmNewPerson = result.needs_confirmation === true;
        confirmPinReset = result.needs_reset_confirmation === true;
      }
      if (!response.ok && response.status !== 409) {
        throw new Error(result.error || "人員資料儲存失敗，請稍後重試。");
      }

      const faceImageBase64 = await captureFacePhoto();
      enrollmentStatus.textContent = "照片已拍攝，正在安全儲存人員資料…";
      response = await submitPerson(
        personForm,
        name,
        pin,
        confirmNewPerson,
        confirmPinReset,
        faceImageBase64,
      );
      result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "人員資料儲存失敗，請稍後重試。");
      }

      const memberList = document.getElementById("member-list");
      if (memberList) {
        const existingItem = Array.from(memberList.children).find(
          (item) =>
            item.dataset.name.toLocaleLowerCase() ===
            result.name.toLocaleLowerCase(),
        );
        const item = document.createElement("li");
        item.dataset.name = result.name;
        item.dataset.personId = result.person_id;
        item.dataset.hasFaceSample = "true";
        const memberLabel = document.createElement("span");
        memberLabel.textContent = result.name;
        const sampleStatus = document.createElement("small");
        sampleStatus.className = "face-sample-state";
        sampleStatus.textContent = "照片已建檔";
        memberLabel.append(sampleStatus);
        const deleteButton = document.createElement("button");
        deleteButton.className = "camera-button delete-face-photo";
        deleteButton.type = "button";
        deleteButton.dataset.deleteFacePhoto = "";
        deleteButton.textContent = "刪除照片";
        item.append(memberLabel, deleteButton);

        if (existingItem) {
          existingItem.replaceWith(item);
        } else {
          memberList.append(item);
          memberList.hidden = false;
          document.getElementById("member-empty")?.remove();
          const count = document.querySelector(".member-count");
          if (count) {
            count.textContent = `${memberList.children.length} 人`;
          }
        }

        const option = document.createElement("option");
        option.value = result.name;
        const peopleOptions = document.getElementById("registered-people");
        if (
          peopleOptions &&
          !Array.from(peopleOptions.options).some(
            (existingOption) =>
              existingOption.value.toLocaleLowerCase() ===
              result.name.toLocaleLowerCase(),
          )
        ) {
          peopleOptions.append(option);
        }
      }

      personForm.reset();
      stopEnrollmentStream();
      enrollmentCountdown.textContent = "";
      enrollmentStatus.textContent = "人臉照片已存入資料庫；目前尚未啟用身份辨識。";
      personStatus.textContent = `${result.name} 的資料與照片已安全儲存。`;
    } catch (error) {
      personStatus.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    } finally {
      submitButton.disabled = false;
      if (startEnrollmentCamera) {
        startEnrollmentCamera.disabled = Boolean(enrollmentStream);
      }
      if (stopEnrollmentCamera) {
        stopEnrollmentCamera.disabled = !enrollmentStream;
      }
      pinInput.value = "";
    }
  });
}

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
