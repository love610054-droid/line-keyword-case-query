# LINE 關鍵字查案件 Webhook（測試版）

此專案提供 LINE 群組關鍵字查詢功能：
- 群組輸入 `#待處理` 或 `#查案件`
- 系統依群組映射出通路名稱
- 從 Google Sheet 篩選該通路的待處理案件
- 組裝訊息並透過 LINE Messaging API 回覆群組

## 目前狀態

- Webhook 接收與簽名驗證：已完成
- LINE 回覆訊息：已完成
- 群組 -> 通路映射：
  - Google Sheet 對照（可用）
  - 本機備援檔 `data/group_mapping_local.json`（可用）
- 案件資料讀取（Google Sheet）：已接上
- 測試環境（ngrok + 本機 FastAPI）：已打通

## 專案結構

- `fastapi_main.py`：FastAPI 入口與 `/api/line-webhook`
- `data/line_oa_webhook.py`：Webhook 核心流程（事件過濾、映射、查案件、回覆）
- `data/group_mapping_sync.py`：群組對照讀寫（Google Sheet + 本機備援）
- `data/sheet_sync.py`：案件資料讀取（Google Sheets API）
- `data/group_mapping_local.json`：本機對照備援

## 必要環境變數

請建立 `.env`：

```env
LINE_OA_CHANNEL_SECRET=
LINE_OA_CHANNEL_ACCESS_TOKEN=

GOOGLE_SHEET_ID=
GOOGLE_APPLICATION_CREDENTIALS=

SHEET_TAB_CASES=工作表1
SHEET_TAB_GROUP_MAPPING=群組通路對照
```

說明：
- `GOOGLE_APPLICATION_CREDENTIALS` 為本機 Service Account JSON 檔案路徑
- 請將該 Service Account 的 `client_email` 加入試算表分享（Editor）
- 請確認 GCP 專案已啟用 Google Sheets API

## 安裝與啟動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

set -a
source .env
set +a
uvicorn fastapi_main:app --host 0.0.0.0 --port 8001 --reload
```

## LINE Webhook 設定

1. 使用 ngrok 暴露本機：
   ```bash
   ngrok http 8001
   ```
2. 在 LINE Developers 設定 Webhook URL：
   - `https://<你的-ngrok-網域>/api/line-webhook`
3. 啟用 `Use webhook` 並按 `Verify`

## 功能流程

1. 接收 LINE webhook
2. 驗證 `X-Line-Signature`
3. 過濾群組文字訊息與關鍵字
4. 解析 `group_id -> channel_name`
5. 查詢 Google Sheet（狀態為 `待處理` / `補件中`）
6. 回覆案件列表訊息

## 已知注意事項

- 若回覆「目前沒有待處理案件」，請先檢查：
  - 群組對照是否命中正確通路
  - 案件表分頁與欄位是否正確
  - Service Account 是否有權限讀取表單
- 若出現 `SERVICE_DISABLED`，請至 GCP 啟用 Google Sheets API
- 不要將 `.env` 或憑證 JSON 提交到 Git

## 交接建議（給正式環境）

- 將此模組合併到正式服務（FastAPI 主程式）
- 將本機備援對照改為正式資料庫（如 SQLite/Redis/PostgreSQL）
- 增加監控與告警（Webhook 失敗率、回覆失敗率）
- 增加單元測試與整合測試
