import hashlib
import hmac
import base64
import re
from datetime import date, datetime
from typing import Optional

import httpx

from data.group_mapping_sync import group_mapping_sync
from data.logger import log_print
from data.rule_manager import rule_manager
from data.sheet_sync import sheet_sync

# 群組 ID → 通路名稱的快取，避免重複呼叫 LINE API
_group_channel_cache: dict[str, str] = {}

# 數字 emoji 對照表（1–10）
_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# 觸發關鍵字
_TRIGGER_KEYWORDS = {"#待處理", "#查案件"}


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """用 HMAC-SHA256 驗證 LINE webhook 簽名。"""
    try:
        hash_digest = hmac.new(
            channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(hash_digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        log_print(f"[line_oa_webhook] verify_signature 錯誤: {e}")
        return False


def handle_webhook_event(event: dict, channel_access_token: str) -> None:
    """解析並處理單一 LINE webhook event。

    只處理群組內的文字訊息，且訊息須完全符合觸發關鍵字。
    """
    try:
        if event.get("type") != "message":
            return
        message = event.get("message", {})
        if message.get("type") != "text":
            return
        source = event.get("source", {})
        if source.get("type") != "group":
            return

        text = message.get("text", "").strip()
        if text not in _TRIGGER_KEYWORDS:
            return

        group_id = source.get("groupId", "")
        reply_token = event.get("replyToken", "")
        if not group_id or not reply_token:
            log_print("[line_oa_webhook] 缺少 groupId 或 replyToken，略過")
            return

        channel_name = get_group_channel_name(group_id, channel_access_token)
        if not channel_name:
            log_print(f"[line_oa_webhook] 無法識別群組 {group_id} 對應的通路，略過")
            return

        cases = get_pending_cases(channel_name)
        reply_message = build_reply_message(channel_name, cases)
        reply_to_line(reply_token, reply_message, channel_access_token)

    except Exception as e:
        log_print(f"[line_oa_webhook] handle_webhook_event 未預期錯誤: {e}")


def get_group_channel_name(group_id: str, channel_access_token: str) -> Optional[str]:
    """解析群組對應的通路名稱。

    順序：1) Google Sheet／本機對照表 2) 轉發規則關鍵字 3) 群組名稱推導。
    若由 2) 或 3) 得到通路，會 upsert 寫回對照表供下次直接使用。
    """
    if group_id in _group_channel_cache:
        return _group_channel_cache[group_id]

    mapped = group_mapping_sync.get_channel_by_group_id(group_id)
    if mapped:
        _group_channel_cache[group_id] = mapped
        log_print(f"[line_oa_webhook] 對照表命中：群組 {group_id} → 通路「{mapped}」")
        return mapped

    group_name = _fetch_group_name(group_id, channel_access_token)
    if not group_name:
        return None

    channel_name = _match_channel_from_group_name(group_name)
    if not channel_name:
        channel_name = _guess_channel_from_group_name(group_name)

    if not channel_name:
        log_print(f"[line_oa_webhook] 無法從群組名稱「{group_name}」取得通路")
        return None

    group_mapping_sync.upsert_mapping(group_id, group_name, channel_name)
    _group_channel_cache[group_id] = channel_name
    log_print(
        f"[line_oa_webhook] 群組「{group_name}」→ 通路「{channel_name}」（已寫入對照並快取）"
    )
    return channel_name


def _guess_channel_from_group_name(group_name: str) -> Optional[str]:
    """依群組名稱粗抓通路（例：七方通訊行x瑪吉pay → 七方通訊行）。"""
    s = group_name.strip()
    if not s:
        return None
    if re.search(r"[xX]", s):
        part = re.split(r"[xX]", s, maxsplit=1)[0].strip()
        if part:
            # 去掉開頭門市代號，例如 AC137
            part = re.sub(r"^\s*[A-Za-z]{1,4}\d+\s+", "", part).strip()
            return part or None
    return s


def _fetch_group_name(group_id: str, channel_access_token: str) -> Optional[str]:
    """呼叫 LINE API 取得群組摘要，回傳群組名稱。"""
    url = f"https://api.line.me/v2/bot/group/{group_id}/summary"
    headers = {"Authorization": f"Bearer {channel_access_token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("groupName", "")
        log_print(f"[line_oa_webhook] 取得群組名稱失敗，status={resp.status_code}, body={resp.text}")
        return None
    except Exception as e:
        log_print(f"[line_oa_webhook] _fetch_group_name 錯誤: {e}")
        return None


def _match_channel_from_group_name(group_name: str) -> Optional[str]:
    """從轉發規則中，找出符合群組名稱的通路名稱。

    遍歷所有規則的 condition_keywords，若某個關鍵字包含於群組名稱中，
    即視為匹配，回傳該規則對應的通路名稱。
    """
    try:
        rules = rule_manager.get_rules() if hasattr(rule_manager, "get_rules") else []
        for rule in rules:
            keywords = rule.get("condition_keywords", [])
            for kw in keywords:
                if kw and kw in group_name:
                    # 使用第一個關鍵字作為通路名稱（通常就是通路的顯示名稱）
                    channel_name = rule.get("channel_name") or kw
                    return channel_name
    except Exception as e:
        log_print(f"[line_oa_webhook] _match_channel_from_group_name 錯誤: {e}")
    return None


def get_pending_cases(channel_name: str) -> list[dict]:
    """從 Google Sheet 讀取指定通路的待處理 / 補件中案件。

    回傳案件列表，每筆包含 date, case_id, name, product, status, days_pending。
    """
    pending_statuses = {"待處理", "補件中"}
    today = date.today()
    results = []

    try:
        all_rows = sheet_sync._get_all_rows()
        for row in all_rows:
            # row 為 list，對應欄位：A=0 B=1 C=2 D=3 E=4 F=5 G=6 H=7 I=8
            if len(row) < 6:
                continue
            row_channel = str(row[1]).strip()
            row_status = str(row[5]).strip()
            if row_channel != channel_name or row_status not in pending_statuses:
                continue

            raw_date = str(row[0]).strip()
            case_id = str(row[2]).strip()
            name = str(row[3]).strip()
            product = str(row[4]).strip()

            days_pending = _calc_days_pending(raw_date, today)
            results.append(
                {
                    "date": raw_date,
                    "case_id": case_id,
                    "name": name,
                    "product": product,
                    "status": row_status,
                    "days_pending": days_pending,
                }
            )
    except Exception as e:
        log_print(f"[line_oa_webhook] get_pending_cases 錯誤: {e}")

    return results


def _calc_days_pending(raw_date: str, today: date) -> int:
    """將日期字串解析為 date 後計算距今天數，解析失敗回傳 0。"""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            case_date = datetime.strptime(raw_date, fmt).date()
            delta = (today - case_date).days
            return max(delta, 0)
        except ValueError:
            continue
    return 0


def build_reply_message(channel_name: str, cases: list[dict]) -> str:
    """組裝要回覆到 LINE 群組的文字訊息。"""
    if not cases:
        return "🎉 太棒了！目前沒有待處理的案件～\n繼續保持💪"

    lines = [
        "🔔 案件追蹤小提醒～\n",
        f"📋 目前還有 {len(cases)} 筆案件等待處理唷！\n",
    ]

    for i, case in enumerate(cases, start=1):
        if i <= len(_NUMBER_EMOJIS):
            prefix = _NUMBER_EMOJIS[i - 1]
        else:
            prefix = f"({i})"

        days = case.get("days_pending", 0)
        status = case.get("status", "")
        case_id = case.get("case_id", "")
        name = case.get("name", "")
        lines.append(f"{prefix} {case_id} {name} — {status}（{days}天）")

    lines.append(
        "\n⏰ 客戶熱度很重要～幫忙小助理趁早完成核准目標吧 🥳\n"
        "如果需要小助理幫忙協調或業務爭取的，快快跟我們說 別客氣😊"
    )

    return "\n".join(lines)


def reply_to_line(reply_token: str, message: str, channel_access_token: str) -> None:
    """透過 LINE Messaging API 回覆訊息。

    reply_token 只能使用一次且 30 秒內有效，請盡快呼叫。
    """
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {channel_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": message}],
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            log_print("[line_oa_webhook] 回覆成功")
        else:
            log_print(
                f"[line_oa_webhook] 回覆失敗，status={resp.status_code}, body={resp.text}"
            )
    except Exception as e:
        log_print(f"[line_oa_webhook] reply_to_line 錯誤: {e}")
