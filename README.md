# FaceSense AI 人臉偵測系統

使用 Flask 製作的 AI 人臉偵測展示介面。控制台統計與最近活動仍是示意資料；即時人臉偵測可啟用瀏覽器攝影機，將單張 JPEG 影像送至 Face Detection API。

即時功能只偵測臉部位置，不會辨識姓名、建立人員資料或記錄打卡。影像不會由此 Flask 應用程式保存。瀏覽器攝影機須在 HTTPS 或 localhost 中使用。

偵測回應目前以 JSON 原文顯示；供應商的 OpenAPI 文件沒有描述回應欄位結構，因此尚未繪製臉部框。後端另設每個來源 IP 每分鐘 10 次的程序內限流；此限制會在程序重啟時重設，多個應用程式執行個體不會共用計數。

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

## 人臉偵測設定

設定伺服器環境變數 `FACE_DETECT_API_KEY`，Flask 後端會以 `x-api-key` 標頭呼叫 `https://api-face-detect.knowlez.com/v1/face/detect`。金鑰不會送到瀏覽器或存入儲存庫。

本機測試可在目前 PowerShell 工作階段設定：

```powershell
$env:FACE_DETECT_API_KEY = "由服務供應商簽發的有效金鑰"
python app.py
```

部署到 Azure 前，請在 Azure Portal 的 Web App → **Settings → Environment variables** 加入 `FACE_DETECT_API_KEY`，儲存後確認應用程式已重新啟動。任何曾貼到聊天、提交到 Git 或其他不受信任位置的金鑰都應先撤銷，再使用新金鑰。

## 部署到 Azure Web App

`.github/workflows/deploy-azure.yml` 在 `main` 分支的應用程式、測試或 workflow 異動時執行，也可以從 GitHub Actions 手動觸發。流程會在 Windows runner 上執行測試、下載官方 Python 3.14.8 可嵌入版本，將 Python 與相依套件一起部署到 `NTUSTweb00`（資源群組 `NTUSTlab2026`）。站點透過 `web.config` 的 IIS HttpPlatformHandler 啟動 `serve.py`，以 Waitress 提供 Flask 服務。

此方法使用**隨部署包提供的 Python**，不依賴 Windows App Service 內建 Python 執行環境；HttpPlatformHandler 必須在目標站點可用。`NTUSTweb00` 目前是 Windows Web App，部署時會確認作業系統。

在 GitHub 儲存庫 **Settings → Secrets and variables → Actions** 確認：

- Repository secret `AZURE_CREDENTIALS`：Azure 服務主體的 `clientId`、`clientSecret`、`subscriptionId`、`tenantId` 組成的 JSON。此 Secret 已設定，不要把內容寫進程式碼。

請確認該服務主體對 `NTUSTweb00` 具有部署權限（例如該站點的 **Website Contributor**）。推送符合上述條件的 `main` 分支異動，或從 GitHub Actions 手動執行工作流程即可部署。
