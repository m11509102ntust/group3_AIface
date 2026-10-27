# FaceSense AI 人臉照片建檔與打卡系統

使用 Flask 製作的人臉照片建檔與 PIN 打卡系統。控制台出勤統計取自 SQLite 資料庫，並依登入角色限制可見範圍；打卡紀錄、人員名冊、人臉照片及待審申請都儲存在 SQLite。

本人先啟動瀏覽器攝影機並倒數 5 秒拍照，預覽照片後再填寫姓名、個人 PIN 並明確同意，提交建檔申請。照片與 PIN 雜湊先存於待審資料；管理員核准後才會在同一筆資料庫交易中建立人員名冊與人臉照片，並允許使用該 PIN 打卡。管理員拒絕時會刪除整筆待審資料。照片不會送到外部偵測服務。此功能只建立後續可用的照片資料，尚未實作身份辨識或自動打卡。瀏覽器攝影機須在 HTTPS 或 localhost 中使用。

每位人員仍以姓名和個人 PIN 登入後，才能記錄自己的打卡及查看自己的紀錄。管理員使用獨立密碼審核申請、管理名冊及刪除照片；同名的既有人員或待審申請不可重複提交，且未核准者不能登入打卡。PIN 以 PBKDF2 雜湊儲存，不會保存明文。僅在取得本人明確同意後建檔；SQLite 資料庫及其備份均須依人臉資料的敏感程度妥善控管。

## 啟動

需安裝 Python 3.9 以上版本。在專案資料夾執行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

接著開啟 http://127.0.0.1:5000/ 。若 PowerShell 不允許啟用虛擬環境，也可以使用 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` 和 `.\.venv\Scripts\python.exe app.py`。

第一次啟動前，請在專案根目錄複製範例設定並用記事本填入自己的密碼與秘密金鑰：

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 已加入 Git 忽略清單，不要提交或分享此檔案。`CHECKIN_ADMIN_PASSWORD` 是管理員登入密碼；個人 PIN 則由每位人員建檔時自行設定，系統只會將 PIN 雜湊存進資料庫，無須在 `.env` 設定。

執行測試：`python -m unittest discover -s tests`

##正式站: https://ntustwebtest18.azurewebsites.net/

## 打卡登入與 SQLite

本機打卡頁面提供個人打卡登入及管理員人員管理。應用程式啟動時會從專案根目錄 `.env` 載入設定；作業系統已設定的環境變數優先於 `.env`：

- `CHECKIN_ADMIN_PASSWORD`：強且獨有的管理員密碼。知道此密碼的人可登入管理員頁面，核准或拒絕建檔申請、管理名冊及刪除照片；此密碼不會用於一般人員打卡。
- `FLASK_SECRET_KEY`：Flask 簽署登入 session 的隨機秘密；請使用高熵值，勿重複使用 API key 或提交至 Git。
- `CHECKIN_DB_PATH`（選用）：SQLite 檔案的完整路徑。未設定時，本機使用 Flask `instance` 資料夾；Azure Windows App Service 使用 `D:\home\data\checkins.sqlite3`。

本人提交建檔申請後，申請會先進入待審狀態；此時尚未建立可登入的人員。管理員登入後可檢視照片及申請姓名，再選擇核准或拒絕。核准時姓名、PIN 雜湊與照片會一起建立；拒絕時待審資料會刪除。人員以已核准的姓名與 PIN 登入後，打卡紀錄由登入的 person ID 決定，不能從瀏覽器指定其他姓名。個人 PIN 依姓名、管理員登入依管理員帳號，皆限制每個程序 15 分鐘內最多 5 次嘗試；程序重啟或多個執行個體不會共用計數。員工若看見「姓名或 PIN 不正確」，請確認申請是否已由管理員核准。

產生 `FLASK_SECRET_KEY` 的方式（請在自己的終端機執行，將輸出貼入本機 `.env`，不要貼到聊天或提交至儲存庫）：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

資料庫會建立 `people` 人員名冊（含個人 PIN 雜湊）、`enrollment_requests` 待審申請表、`face_samples` 人臉照片表，以及 `mytable` 打卡資料表（包含 `姓名`、`打卡時間` 欄位）。每筆核准與打卡均以資料庫交易記錄。時間以 UTC ISO-8601 格式儲存，頁面依瀏覽器時區顯示。Azure 預設路徑位於持久化的 `D:\home`，不要將資料庫指定到部署目錄；若覆寫 `CHECKIN_DB_PATH`，仍須選用 Azure 持久資料目錄並自行安排備份。

總覽出勤統計由實際打卡資料計算。管理員可查看全體今日打卡筆數、今日不重複打卡人數及名冊人數；員工只會看到自己的打卡統計，未登入者不會看到出勤數字。照片存於 SQLite 的 `face_samples` 表，以人員 ID 為主鍵；重新拍照會取代既有照片，管理員可從人員名冊刪除照片。

身份辨識目前未實作。既有 `FACE_DETECT_API_KEY` 對應的是臉部位置偵測服務，不支援此應用所需的人員照片建檔或身份比對；本程式不再呼叫該服務。要啟用辨識，需另行選定支援身份比對的服務，並設計資料保存與刪除流程。

本機 `.env` 僅供本機程式載入，部署流程不會打包 `.env`，GitHub Actions 也無法讀取你電腦上的本機檔案。若要讓 Azure 正式網站也能使用管理員密碼，請在 GitHub 儲存庫 **Settings → Secrets and variables → Actions → New repository secret** 新增 `CHECKIN_ADMIN_PASSWORD`，值設為與本機 `.env` 相同的密碼。部署 workflow 會透過 GitHub Actions Secret 自動更新 Azure App Service 設定；不必手動進 Azure Portal 設定，也不要將 `.env` 或密碼提交至 Git。知道該密碼的人可在網站登入管理員並管理人員。

## 人臉照片建檔與管理員審核

本人在「先拍照，再提交建檔」區啟動攝影機，按下拍照後倒數 5 秒，檢視照片預覽，再填寫姓名、6 至 12 位數字 PIN 並勾選明確同意，最後提交申請。拍攝照片在提交前只保留於目前瀏覽器記憶體，不會上傳。申請送出後，照片和 PIN 雜湊存入 `enrollment_requests`，但申請者尚不能打卡。

管理員在「待審建檔申請」檢視人員姓名與照片後核准或拒絕。核准會將人員與照片一起新增至正式名冊；同名既有人員會被拒絕核准，避免覆寫帳號。拒絕會刪除待審資料。核准後管理員也可從「已登記人員」刪除照片。每張照片上限 5 MB。

資料庫的待審申請與 `face_samples` 表保存照片二進位內容、MIME 類型、建立時間及同意確認時間。照片是敏感個人資料，請限制資料庫及備份存取，並依實際需求設定保留期間。現階段不建立臉部特徵、不執行身份比對；`FACE_DETECT_API_KEY` 只是既有位置偵測 API 金鑰，不能用於身份辨識，也不會被本流程呼叫。

## 部署到 Azure Web App

`.github/workflows/deploy-azure.yml` 在 `main` 分支的應用程式、測試或 workflow 異動時執行，也可以從 GitHub Actions 手動觸發。流程會在 Windows runner 上執行測試、下載官方 Python 3.14.8 可嵌入版本，將 Python 與相依套件一起部署到 Azure Web App（由儲存庫變數 `AZURE_WEBAPP_NAME` 與 `AZURE_RESOURCE_GROUP` 指定）。站點透過 `web.config` 的 IIS HttpPlatformHandler 啟動 `serve.py`，以 Waitress 提供 Flask 服務。

此方法使用**隨部署包提供的 Python**，不依賴 Windows App Service 內建 Python 執行環境；HttpPlatformHandler 必須在目標站點可用。部署時會確認目標是 Windows Web App。

在 GitHub 儲存庫 **Settings → Secrets and variables → Actions** 確認：

- Repository secret `AZURE_CREDENTIALS`：Azure 服務主體的 `clientId`、`clientSecret`、`subscriptionId`、`tenantId` 組成的 JSON。此 Secret 已設定，不要把內容寫進程式碼。
- Repository secret `CHECKIN_ADMIN_PASSWORD`：與本機 `.env` 相同的管理員密碼；部署 workflow 會將其套用至 Azure App Service，不需在 Azure Portal 手動設定。
- Repository secret `FLASK_SECRET_KEY`：Flask session 簽署金鑰。
- Repository secret `FACE_DETECT_API_KEY`：部署 workflow 所需的既有 API 金鑰。

請確認該服務主體對指定的 Web App 具有部署權限（例如該站點的 **Website Contributor**）。推送符合上述條件的 `main` 分支異動，或從 GitHub Actions 手動執行工作流程即可部署。
