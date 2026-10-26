# FaceSense AI 人臉照片建檔與打卡系統

使用 Flask 製作的人臉照片建檔與 PIN 打卡系統。控制台出勤統計取自 SQLite 資料庫，並依登入角色限制可見範圍；打卡紀錄、人員名冊及人臉照片都儲存在 SQLite。

管理員新增人員或確認重設 PIN 時，可啟動瀏覽器攝影機；確認本人同意後，系統倒數 5 秒拍攝單張照片，並在同一筆資料庫交易中保存人員與照片。每位人員只保存一張照片；重新建檔會取代舊照片，管理員也可刪除照片。照片不會送到外部偵測服務。此功能只建立後續可用的照片資料，尚未實作身份辨識或自動打卡。瀏覽器攝影機須在 HTTPS 或 localhost 中使用。

每位人員仍以姓名和個人 PIN 登入後，才能記錄自己的打卡及查看自己的紀錄。管理員使用獨立密碼管理名冊；未登記人員不能自行註冊或占用姓名。PIN 以 PBKDF2 雜湊儲存，不會保存明文。僅在取得本人明確同意後建檔；SQLite 資料庫及其備份均須依人臉資料的敏感程度妥善控管。

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

##正式站: https://ntustwebtest18.azurewebsites.net/

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

總覽出勤統計由實際打卡資料計算。管理員可查看全體今日打卡筆數、今日不重複打卡人數及名冊人數；員工只會看到自己的打卡統計，未登入者不會看到出勤數字。照片存於 SQLite 的 `face_samples` 表，以人員 ID 為主鍵；重新拍照會取代既有照片，管理員可從人員名冊刪除照片。

身份辨識目前未實作。既有 `FACE_DETECT_API_KEY` 對應的是臉部位置偵測服務，不支援此應用所需的人員照片建檔或身份比對；本程式不再呼叫該服務。要啟用辨識，需另行選定支援身份比對的服務，並設計資料保存與刪除流程。

Azure Portal 的 Web App → **Settings → Environment variables** 設定 `CHECKIN_ADMIN_PASSWORD`、`FLASK_SECRET_KEY`，可選擇設定 `CHECKIN_DB_PATH` 為 `D:\home\data\checkins.sqlite3`。儲存設定後確認應用程式已重新啟動。這些設定是 Azure App Service 設定，不是 GitHub Actions secrets，也不應寫入 workflow 或程式碼。

## 人臉照片建檔

管理員登入後，在「人員打卡」區啟動攝影機，輸入姓名與 PIN，並確認本人已明確同意保存照片。新姓名或既有姓名重設 PIN 都須先確認；確認後系統倒數 5 秒拍照，再將照片和人員資料一起寫入 SQLite。每張照片上限 5 MB。重新建檔會取代該人員原照片；若要刪除，請在「已登記人員」按「刪除照片」。

資料庫的 `face_samples` 表保存照片二進位內容、MIME 類型、建立時間及同意確認時間。照片是敏感個人資料，請限制資料庫及備份存取，並依實際需求設定保留期間。現階段不建立臉部特徵、不執行身份比對；`FACE_DETECT_API_KEY` 只是既有位置偵測 API 金鑰，不能用於身份辨識，也不會被本流程呼叫。

## 部署到 Azure Web App

`.github/workflows/deploy-azure.yml` 在 `main` 分支的應用程式、測試或 workflow 異動時執行，也可以從 GitHub Actions 手動觸發。流程會在 Windows runner 上執行測試、下載官方 Python 3.14.8 可嵌入版本，將 Python 與相依套件一起部署到 Azure Web App（由儲存庫變數 `AZURE_WEBAPP_NAME` 與 `AZURE_RESOURCE_GROUP` 指定）。站點透過 `web.config` 的 IIS HttpPlatformHandler 啟動 `serve.py`，以 Waitress 提供 Flask 服務。

此方法使用**隨部署包提供的 Python**，不依賴 Windows App Service 內建 Python 執行環境；HttpPlatformHandler 必須在目標站點可用。部署時會確認目標是 Windows Web App。

在 GitHub 儲存庫 **Settings → Secrets and variables → Actions** 確認：

- Repository secret `AZURE_CREDENTIALS`：Azure 服務主體的 `clientId`、`clientSecret`、`subscriptionId`、`tenantId` 組成的 JSON。此 Secret 已設定，不要把內容寫進程式碼。

請確認該服務主體對指定的 Web App 具有部署權限（例如該站點的 **Website Contributor**）。推送符合上述條件的 `main` 分支異動，或從 GitHub Actions 手動執行工作流程即可部署。
