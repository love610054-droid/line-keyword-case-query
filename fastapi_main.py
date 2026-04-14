# ======================================================================
# 此檔案僅包含 LINE OA Webhook 相關的新增段落。
# 請將下方的 import 與 endpoint 合併到現有的 fastapi_main.py 中。
# ======================================================================

import json
import os

from fastapi import FastAPI, HTTPException, Request

from data.line_oa_webhook import (
    handle_webhook_event,
    verify_signature,
)

app = FastAPI()

# ── 現有的其他 endpoints 放在這裡（保持原樣不動）──────────────────────


# ── 新增：LINE OA Webhook ──────────────────────────────────────────────
@app.post("/api/line-webhook")
async def line_webhook(request: Request):
    """接收 LINE Messaging API 的 webhook 事件。

    驗證簽名後依序處理每個 event，任何錯誤都不會讓 LINE 收到 500，
    以防 LINE 無限重試。
    """
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    channel_secret = os.environ.get("LINE_OA_CHANNEL_SECRET", "")
    if not channel_secret:
        # 未設定 secret 時拒絕所有請求，避免安全漏洞
        raise HTTPException(status_code=500, detail="LINE_OA_CHANNEL_SECRET not configured")

    if not verify_signature(body, signature, channel_secret):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        events = json.loads(body).get("events", [])
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    channel_access_token = os.environ.get("LINE_OA_CHANNEL_ACCESS_TOKEN", "")

    for event in events:
        handle_webhook_event(event, channel_access_token)

    return {"status": "ok"}
