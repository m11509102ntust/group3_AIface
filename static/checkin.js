const checkinForm = document.getElementById("checkin-form");
const checkinStatus = document.getElementById("checkin-status");
const personForm = document.getElementById("person-form");
const personStatus = document.getElementById("person-status");
const pendingFaceList = document.getElementById("pending-face-list");

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

if (pendingFaceList) {
  pendingFaceList.addEventListener("click", async (event) => {
    const selectButton = event.target.closest("[data-select-pending-face]");
    if (selectButton) {
      const card = selectButton.closest("[data-request-id]");
      const requestId = card.dataset.requestId;
      document.getElementById("pending-face-request-id").value = requestId;
      const preview = document.getElementById("selected-face-preview");
      const image = document.getElementById("selected-face-image");
      image.src = card.querySelector("img").src;
      preview.hidden = false;
      document.getElementById("selected-face-status").textContent =
        "已同步待審照片；請填寫姓名與 PIN。";
      personStatus.textContent = "待審照片已同步到新增人員表單。";
      document.getElementById("person-name").focus();
      return;
    }

    const button = event.target.closest(".reject-face-request");
    if (!button) {
      return;
    }
    const item = button.closest("li");
    const requestId = encodeURIComponent(item.dataset.requestId);
    const status = document.getElementById("face-request-status");
    button.disabled = true;
    status.textContent = "正在拒絕申請…";
    try {
      const response = await fetch(
        `/api/face/enrollment-requests/${requestId}`,
        {
          method: "DELETE",
          headers: {
            "X-CSRF-Token": pendingFaceList.dataset.csrfToken,
          },
        },
      );
      const result = await readJsonResponse(response, "審核操作失敗。");
      if (!response.ok) {
        throw new Error(result.error || "審核操作失敗。");
      }
      item.remove();
      status.textContent = "待審照片已拒絕並刪除。";
    } catch (error) {
      button.disabled = false;
      status.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    }
  });
}

const batchApproveButton = document.getElementById("batch-approve-face");
if (batchApproveButton && pendingFaceList) {
  batchApproveButton.addEventListener("click", async () => {
    const entries = Array.from(
      pendingFaceList.querySelectorAll(
        'input[name="batch_face_request"]:checked',
      ),
    ).map((checkbox) => {
      const card = checkbox.closest("[data-request-id]");
      return {
        request_id: Number(card.dataset.requestId),
        name: card.querySelector(".batch-face-name").value.trim(),
        pin: card.querySelector(".batch-face-pin").value,
      };
    });
    if (!entries.length) {
      personStatus.textContent = "請先勾選要批次核准的人臉照片。";
      return;
    }

    batchApproveButton.disabled = true;
    personStatus.textContent = "正在批次核准並建立人臉模型…";
    try {
      const response = await fetch(
        "/api/face/enrollment-requests/batch-approve",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": pendingFaceList.dataset.csrfToken,
          },
          body: JSON.stringify({ entries }),
        },
      );
      const result = await readJsonResponse(response, "批次核准失敗。");
      if (!response.ok) {
        throw new Error(result.error || "批次核准失敗。");
      }
      for (const approved of result.approved) {
        pendingFaceList
          .querySelector(`[data-request-id="${approved.request_id}"]`)
          ?.remove();
      }
      const failedText =
        result.failed.length
          ? `；失敗 ${result.failed.length} 筆：${result.failed.map((item) => item.error).join("、")}`
          : "";
      personStatus.textContent =
        `批次核准完成：成功 ${result.approved.length} 筆，待審照片已移除${failedText}`;
      if (!pendingFaceList.children.length) {
        document.getElementById("pending-face-sync").hidden = true;
      }
    } catch (error) {
      personStatus.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    } finally {
      batchApproveButton.disabled = false;
    }
  });
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

async function readJsonResponse(response, fallbackMessage) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return { error: fallbackMessage };
  }
}

if (checkinForm) {
  checkinForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = checkinForm.querySelector("button[type='submit']");
    submitButton.disabled = true;
    checkinStatus.textContent = "正在儲存打卡資料…";

    try {
      const response = await createCheckin(checkinForm.dataset.csrfToken);
      const result = await readJsonResponse(response, "打卡失敗，請稍後重試。");
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
  if (personForm.dataset.autoFaceEnroll === "true") {
    fetch("/api/face/auto-enroll", {
      method: "POST",
      headers: { "X-CSRF-Token": personForm.dataset.csrfToken },
    })
      .then(async (response) => {
        const result = await readJsonResponse(response, "照片自動建模失敗。");
        if (response.status === 409) {
          return;
        }
        if (!response.ok) {
          throw new Error(result.error || "照片自動建模失敗。");
        }
        if (result.enrolled > 0 || result.errors.length > 0) {
          personStatus.textContent = `照片自動建模完成：${result.enrolled} 人成功。${
            result.errors.length > 0 ? ` ${result.errors.join("；")}` : ""
          }`;
        }
      })
      .catch((error) => {
        personStatus.textContent = error.message;
      });
  }

  personForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const nameInput = personForm.elements.namedItem("name");
    const pinInput = personForm.elements.namedItem("pin");
    const submitButton = personForm.querySelector("button[type='submit']");
    const name = nameInput.value.trim();
    const pin = pinInput.value;
    submitButton.disabled = true;
    personStatus.textContent = "正在儲存人員資料…";

    try {
      const pendingRequestId = personForm.elements.namedItem(
        "pending_face_request_id",
      )?.value;
      if (pendingRequestId) {
        personStatus.textContent = "正在核准照片、裁切人臉並建立模型…";
        const response = await fetch(
          `/api/face/enrollment-requests/${encodeURIComponent(pendingRequestId)}/approve`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": personForm.dataset.csrfToken,
            },
            body: JSON.stringify({ name, pin }),
          },
        );
        const result = await readJsonResponse(response, "待審人臉建檔失敗。");
        if (!response.ok) {
          throw new Error(result.error || "待審人臉建檔失敗。");
        }
        personForm.reset();
        document.getElementById("selected-face-preview").hidden = true;
        document
          .querySelector(`[data-request-id="${pendingRequestId}"]`)
          ?.remove();
        personStatus.textContent =
          `${result.name} 已核准，照片已裁切並建立人臉模型。`;
        window.location.reload();
        return;
      }

      const submitPerson = async (confirmNewPerson, confirmPinReset) =>
        fetch("/api/people", {
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
          }),
        });

      let response = await submitPerson(false, false);
      let result = await readJsonResponse(response, "人員資料儲存失敗。");
      if (
        response.status === 409 &&
        (result.needs_confirmation || result.needs_reset_confirmation)
      ) {
        if (!window.confirm(result.message)) {
          personStatus.textContent = "已取消變更人員資料。";
          return;
        }
        response = await submitPerson(
          result.needs_confirmation === true,
          result.needs_reset_confirmation === true,
        );
        result = await readJsonResponse(response, "人員資料儲存失敗。");
      }
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
        if (existingItem) {
          existingItem.dataset.name = result.name;
          existingItem.textContent = result.name;
        } else {
          const item = document.createElement("li");
          item.dataset.name = result.name;
          item.textContent = result.name;
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
        document.getElementById("registered-people")?.append(option);
      }

      personForm.reset();
      personStatus.textContent = `${result.name} 的 PIN 已安全儲存。`;
    } catch (error) {
      personStatus.textContent =
        error instanceof TypeError
          ? "無法連線至網站，請檢查網路後重試。"
          : error.message;
    } finally {
      submitButton.disabled = false;
      pinInput.value = "";
    }
  });
}

const registeredPeople = document.getElementById("registered-people");
if (registeredPeople) {
  registeredPeople.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-person-id]");
    if (!button) return;

    const personName = button.dataset.personName || "此人員";
    if (!window.confirm(`確定要刪除「${personName}」及其所有打卡紀錄嗎？`)) {
      return;
    }

    button.disabled = true;
    const status = document.getElementById("registered-status");
    try {
      const response = await fetch(`/api/people/${button.dataset.deletePersonId}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": registeredPeople.dataset.csrfToken || "" },
      });
      const result = await readJsonResponse(response, "刪除人員失敗。");
      if (!response.ok) throw new Error(result.error || "刪除人員失敗。");

      button.closest("li")?.remove();
      const memberList = document.getElementById("member-list");
      if (memberList && memberList.children.length === 0) {
        memberList.hidden = true;
        if (!document.getElementById("member-empty")) {
          const empty = document.createElement("p");
          empty.id = "member-empty";
          empty.className = "empty-state";
          empty.textContent = "尚無人員資料。";
          registeredPeople.append(empty);
        }
      }
      const count = document.querySelector(".member-count");
      if (count) count.textContent = `${memberList?.children.length || 0} 人`;
      if (status) {
        status.textContent =
          result.model_warning || result.azure_warning ||
          `已刪除 ${result.name}，並一併刪除 ${result.deleted_checkins} 筆打卡紀錄。`;
      }
    } catch (error) {
      button.disabled = false;
      if (status) status.textContent = error.message;
    }
  });
}

displayLocalTimes();
