const checkinForm = document.getElementById("checkin-form");
const checkinStatus = document.getElementById("checkin-status");
const personForm = document.getElementById("person-form");
const personStatus = document.getElementById("person-status");

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
    submitButton.disabled = true;
    personStatus.textContent = "正在儲存人員資料…";

    try {
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
      let result = await response.json();
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
        result = await response.json();
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

displayLocalTimes();
