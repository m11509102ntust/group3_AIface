# FaceSense AI 人臉偵測系統

使用 Flask 製作的 AI 人臉偵測及打卡系統。控制台出勤統計取自 SQLite 資料庫，並依登入角色限制可見範圍；打卡紀錄、人員名冊及人臉偵測啟用狀態會儲存在 SQLite。即時人臉偵測可啟用瀏覽器攝影機，將單張 JPEG 影像送至 Face Detection API。

即時功能只偵測臉部位置，不會辨識姓名或自動打卡；目前尚未選定身份辨識服務。每位人員以姓名和個人 PIN 登入後，只能記錄自己的打卡及查看自己的紀錄。管理員使用獨立密碼管理名冊，新增人員或確認後重設其 PIN；未登記人員不能自行註冊或占用姓名。PIN 以 PBKDF2 雜湊儲存，不會保存明文。影像不會由此 Flask 應用程式保存。瀏覽器攝影機須在 HTTPS 或 localhost 中使用。

偵測回應目前以 JSON 原文顯示；供應商的 OpenAPI 文件沒有描述回應欄位結構，因此尚未繪製臉部框。管理員可在系統設定中啟用或停用人臉偵測，但不能在網站讀取或修改 API 金鑰。後端另設每個來源 IP 每分鐘 10 次的程序內限流；此限制會在程序重啟時重設，多個應用程式執行個體不會共用計數。

## 啟動

需安裝 Python 3.9 以上版本。在專案資料夾執行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

接著開啟 http://127.0.0.1:5000/ 。若 PowerShell 不允許啟用虛擬環境，也可以使用 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` 和 `.\.venv\Scripts\python.exe app.py`。

執行測試：`python -m unittest discover -s tests`

## 打卡登入與 SQLite

打卡頁面提供個人打卡登入及管理員人員管理。設定以下伺服器環境變數後，重新啟動應用程式：

- `CHECKIN_ADMIN_PASSWORD`：設定強且獨有的管理員密碼。管理員可新增人員及設定／重設個人 PIN；此密碼不會用於一般人員打卡。
- `FLASK_SECRET_KEY`：Flask 簽署登入 session 的隨機秘密；請使用高熵值，勿重複使用 API key 或提交至 Git。
- `CHECKIN_DB_PATH`（選用）：SQLite 檔案的完整路徑。未設定時，本機使用 Flask `instance` 資料夾；Azure Windows App Service 使用 `D:\home\data\checkins.sqlite3`。

管理員先登入人員管理，輸入姓名與 6 至 12 位數字 PIN；新姓名會先要求確認，已存在姓名會先要求確認重設 PIN。人員以已登記的姓名與 PIN 登入後，打卡紀錄由登入的 person ID 決定，不能從瀏覽器指定其他姓名。個人 PIN 依姓名、管理員登入依管理員帳號，皆限制每個程序 15 分鐘內最多 5 次嘗試；程序重啟或多個執行個體不會共用計數。員工若看見「姓名或 PIN 不正確」，請確認姓名是否已由管理員登記。

可使用 Python 產生秘密值（請在自己的終端機執行，不要將輸出貼到聊天或提交到儲存庫）：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

本機 PowerShell 設定範例：

```powershell
$env:CHECKIN_ADMIN_PASSWORD = "自行設定的管理員密碼"
$env:FLASK_SECRET_KEY = "自行產生的隨機秘密"
python app.py
```

資料庫會建立 `people` 人員名冊（含個人 PIN 雜湊），以及 `mytable` 打卡資料表（包含 `姓名`、`打卡時間` 欄位）。每筆打卡以資料庫交易記錄。時間以 UTC ISO-8601 格式儲存，頁面依瀏覽器時區顯示。Azure 預設路徑位於持久化的 `D:\home`，不要將資料庫指定到部署目錄；若覆寫 `CHECKIN_DB_PATH`，仍須選用 Azure 持久資料目錄並自行安排備份。

總覽出勤統計由實際打卡資料計算。管理員可查看全體今日打卡筆數、今日不重複打卡人數及名冊人數；員工只會看到自己的打卡統計，未登入者不會看到出勤數字。人臉偵測啟用狀態儲存在 SQLite 的 `app_settings` 表。

管理員登入後可在頁面「系統設定」開關人臉偵測。API 金鑰、管理員密碼及 Flask session 金鑰只從 Azure App Service 環境變數讀取，不會由網站表單收集。身份辨識目前未實作；需先選定支援身份比對的服務，設計人臉資料的明確同意、保存與刪除流程後才能啟用。

Azure Portal 的 Web App → **Settings → Environment variables** 設定 `CHECKIN_ADMIN_PASSWORD`、`FLASK_SECRET_KEY`，可選擇設定 `CHECKIN_DB_PATH` 為 `D:\home\data\checkins.sqlite3`。儲存設定後確認應用程式已重新啟動。這些設定是 Azure App Service 設定，不是 GitHub Actions secrets，也不應寫入 workflow 或程式碼。

## 人臉偵測設定

設定伺服器環境變數 `FACE_DETECT_API_KEY`，Flask 後端會以 `x-api-key` 標頭呼叫 `https://api-face-detect.knowlez.com/v1/face/detect`。金鑰不會送到瀏覽器或存入儲存庫。

本機測試可在目前 PowerShell 工作階段設定：

```powershell
$env:FACE_DETECT_API_KEY = "由服務供應商簽發的有效金鑰"
python app.py
```

部署到 Azure 前，請在 Azure Portal 的 Web App → **Settings → Environment variables** 加入 `FACE_DETECT_API_KEY`，儲存後確認應用程式已重新啟動。任何曾貼到聊天、提交到 Git 或其他不受信任位置的金鑰都應先撤銷，再使用新金鑰。

## 部署到 Azure Web App

`.github/workflows/deploy-azure.yml` 在 `main` 分支的應用程式、測試或 workflow 異動時執行，也可以從 GitHub Actions 手動觸發。流程會在 Windows runner 上執行測試、下載官方 Python 3.14.8 可嵌入版本，將 Python 與相依套件一起部署到 Azure Web App（由儲存庫變數 `AZURE_WEBAPP_NAME` 與 `AZURE_RESOURCE_GROUP` 指定）。站點透過 `web.config` 的 IIS HttpPlatformHandler 啟動 `serve.py`，以 Waitress 提供 Flask 服務。

此方法使用**隨部署包提供的 Python**，不依賴 Windows App Service 內建 Python 執行環境；HttpPlatformHandler 必須在目標站點可用。部署時會確認目標是 Windows Web App。

在 GitHub 儲存庫 **Settings → Secrets and variables → Actions** 確認：

- Repository secret `AZURE_CREDENTIALS`：Azure 服務主體的 `clientId`、`clientSecret`、`subscriptionId`、`tenantId` 組成的 JSON。此 Secret 已設定，不要把內容寫進程式碼。

請確認該服務主體對指定的 Web App 具有部署權限（例如該站點的 **Website Contributor**）。推送符合上述條件的 `main` 分支異動，或從 GitHub Actions 手動執行工作流程即可部署。
