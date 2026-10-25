const checkinForm = document.getElementById("checkin-form");
const checkinStatus = document.getElementById("checkin-status");

function displayLocalTimes() {
  document.querySelectorAll("[data-utc-time]").forEach((element) => {
    const date = new Date(element.dataset.utcTime);
    if (!Number.isNaN(date.getTime())) {
      element.textContent = date.toLocaleString();
    }
  });
}

async function submitCheckin(name, csrfToken, confirmNewPerson = false) {
  return fetch("/api/checkins", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ name, confirm_new_person: confirmNewPerson }),
  });
}

if (checkinForm) {
  checkinForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const nameInput = checkinForm.elements.namedItem("name");
    const submitButton = checkinForm.querySelector("button[type='submit']");
    const name = nameInput.value.trim();
    submitButton.disabled = true;
    checkinStatus.textContent = "正在儲存打卡資料…";

    try {
      const csrfToken = checkinForm.dataset.csrfToken;
      let response = await submitCheckin(name, csrfToken);
      let result = await response.json();

      if (response.status === 409 && result.needs_confirmation) {
        if (!window.confirm(result.message)) {
          checkinStatus.textContent = "已取消新增人員及打卡。";
          return;
        }
        response = await submitCheckin(name, csrfToken, true);
        result = await response.json();
      }

      if (!response.ok) {
        throw new Error(result.error || "打卡失敗，請稍後重試。");
      }

      const records = document.getElementById("checkin-records");
      if (records) {
        const row = records.insertRow(0);
        row.insertCell().textContent = result.name;
        const timeCell = row.insertCell();
        const time = document.createElement("time");
        time.dataset.utcTime = result.checkin_time;
        time.textContent = result.checkin_time;
        timeCell.append(time);
        displayLocalTimes();
      }

      const memberList = document.getElementById("member-list");
      const memberNames = Array.from(memberList?.children || []).map(
        (item) => item.textContent.toLocaleLowerCase(),
      );
      if (memberList && !memberNames.includes(result.name.toLocaleLowerCase())) {
        const item = document.createElement("li");
        item.textContent = result.name;
        memberList.append(item);
        memberList.hidden = false;
        const option = document.createElement("option");
        option.value = result.name;
        document.getElementById("registered-people")?.append(option);
        document.getElementById("member-empty")?.remove();
        const count = document.querySelector(".member-count");
        if (count) {
          count.textContent = `${memberList.children.length} 人`;
        }
      }

      checkinForm.reset();
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

displayLocalTimes();
