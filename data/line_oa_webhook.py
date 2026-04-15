import hashlib
import hmac
import base64
import re
from datetime import date, datetime
from typing import Optional, List

import httpx

from data.group_mapping_sync import group_mapping_sync
from data.logger import log_print
from data.sheet_sync import sheet_sync

# 群組 ID → 通路名稱列表的快取
_group_channel_cache: dict[str, List[str]] = {}

# Antify 轉發規則快取（從 API 拉一次）
_antify_rules_cache: list[dict] = []
_antify_rules_loaded: bool = False

# 數字 emoji 對照表（1–10）
_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# 觸發關鍵字
_TRIGGER_KEYWORDS = {"#待處理", "#查案件"}

# Antify API URL（用來拉轉發規則）
import os
_ANTIFY_API_URL = os.environ.get("ANTIFY_API_URL", "https://antify-watcher.legaltrust.me")


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


def _load_antify_rules() -> list[dict]:
    """從 Antify API 拉取轉發規則（快取，只拉一次）。"""
    global _antify_rules_cache, _antify_rules_loaded
    if _antify_rules_loaded:
        return _antify_rules_cache

    try:
        resp = httpx.get(
            f"{_ANTIFY_API_URL}/api/rules",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            _antify_rules_cache = [r for r in data.get("rules", []) if r.get("enabled")]
            _antify_rules_loaded = True
            log_print(f"[line_oa_webhook] 載入 {len(_antify_rules_cache)} 條 Antify 轉發規則")
        else:
            log_print(f"[line_oa_webhook] 拉取 Antify 規則失敗: {resp.status_code}")
    except Exception as e:
        log_print(f"[line_oa_webhook] 拉取 Antify 規則錯誤: {e}")

    return _antify_rules_cache


def handle_webhook_event(event: dict, channel_access_token: str) -> None:
    """解析並處理單一 LINE webhook event。"""
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

        channel_names = get_group_channel_names(group_id, channel_access_token)
        if not channel_names:
            log_print(f"[line_oa_webhook] 無法識別群組 {group_id} 對應的通路，略過")
            return

        # 撈所有通路的待處理案件
        all_cases: dict[str, list[dict]] = {}
        for ch in channel_names:
            cases = get_pending_cases(ch)
            if cases:
                all_cases[ch] = cases

        reply_message = build_reply_message(all_cases)
        reply_to_line(reply_token, reply_message, channel_access_token)

    except Exception as e:
        log_print(f"[line_oa_webhook] handle_webhook_event 未預期錯誤: {e}")


def get_group_channel_names(group_id: str, channel_access_token: str) -> List[str]:
    """解析群組對應的通路名稱（可能多個）。

    使用 Antify 轉發規則的 action_targets 反查 condition_keywords。
    """
    if group_id in _group_channel_cache:
        return _group_channel_cache[group_id]

    # 嘗試從對照表讀取
    mapped = group_mapping_sync.get_channel_by_group_id(group_id)
    if mapped:
        result = [mapped] if isinstance(mapped, str) else mapped
        _group_channel_cache[group_id] = result
        log_print(f"[line_oa_webhook] 對照表命中：群組 {group_id} → {result}")
        return result

    # 從 LINE API 取得群組名稱
    group_name = _fetch_group_name(group_id, channel_access_token)
    if not group_name:
        return []

    # 從 Antify 轉發規則反查通路名稱
    channel_names = _match_channels_from_rules(group_name)

    if not channel_names:
        log_print(f"[line_oa_webhook] 無法從群組名稱「{group_name}」匹配到通路")
        return []

    # 寫入對照表 + 快取（多通路用逗號分隔存）
    group_mapping_sync.upsert_mapping(group_id, group_name, ",".join(channel_names))
    _group_channel_cache[group_id] = channel_names
    log_print(f"[line_oa_webhook] 群組「{group_name}」→ 通路 {channel_names}（已快取）")
    return channel_names


def _match_channels_from_rules(group_name: str) -> List[str]:
    """從 Antify 轉發規則中，反查哪些通路的 action_targets 包含此群組名稱。

    邏輯：遍歷所有規則，若規則的 action_targets 中任一目標被群組名稱包含（或反過來），
    則該規則的 condition_keywords[0] 就是通路名稱。
    """
    rules = _load_antify_rules()
    matched = []
    seen = set()

    for rule in rules:
        targets = rule.get("action_targets", [])
        keywords = rule.get("condition_keywords", [])
        if not targets or not keywords:
            continue

        for target in targets:
            # 精確匹配：群組名稱 == 轉發目標
            if target == group_name:
                channel = keywords[0]
                if channel not in seen:
                    matched.append(channel)
                    seen.add(channel)
                break
            # 模糊匹配：群組名稱包含轉發目標，或反過來
            if target in group_name or group_name in target:
                channel = keywords[0]
                if channel not in seen:
                    matched.append(channel)
                    seen.add(channel)
                break

    return matched


def _fetch_group_name(group_id: str, channel_access_token: str) -> Optional[str]:
    """呼叫 LINE API 取得群組摘要，回傳群組名稱。"""
    url = f"https://api.line.me/v2/bot/group/{group_id}/summary"
    headers = {"Authorization": f"Bearer {channel_access_token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("groupName", "")
        log_print(f"[line_oa_webhook] 取得群組名稱失敗，status={resp.status_code}")
        return None
    except Exception as e:
        log_print(f"[line_oa_webhook] _fetch_group_name 錯誤: {e}")
        return None


def get_pending_cases(channel_name: str) -> list[dict]:
    """從 Google Sheet 讀取指定通路的待處理 / 補件中案件。"""
    pending_statuses = {"待處理", "補件中"}
    today = date.today()
    results = []

    try:
        all_rows = sheet_sync._get_all_rows()
        for row in all_rows:
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
            results.append({
                "date": raw_date,
                "case_id": case_id,
                "name": name,
                "product": product,
                "status": row_status,
                "days_pending": days_pending,
            })
    except Exception as e:
        log_print(f"[line_oa_webhook] get_pending_cases 錯誤: {e}")

    return results


def _calc_days_pending(raw_date: str, today: date) -> int:
    """將日期字串解析為 date 後計算距今天數。"""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            case_date = datetime.strptime(raw_date, fmt).date()
            delta = (today - case_date).days
            return max(delta, 0)
        except ValueError:
            continue
    return 0


def build_reply_message(cases_by_channel: dict[str, list[dict]]) -> str:
    """組裝要回覆到 LINE 群組的文字訊息。

    支援多通路分組顯示。
    """
    if not cases_by_channel:
        return "🎉 太棒了！目前沒有待處理的案件～\n繼續保持💪"

    total = sum(len(cases) for cases in cases_by_channel.values())
    channel_count = len(cases_by_channel)

    lines = [
        "🔔 案件追蹤小提醒～\n",
        f"📋 目前共有 {total} 筆案件等待處理唷！\n",
    ]

    if channel_count == 1:
        # 單通路：不分組，直接列
        channel_name = list(cases_by_channel.keys())[0]
        cases = cases_by_channel[channel_name]
        for i, case in enumerate(cases, start=1):
            prefix = _NUMBER_EMOJIS[i - 1] if i <= len(_NUMBER_EMOJIS) else f"({i})"
            lines.append(
                f"{prefix} {case['case_id']} {case['name']} — {case['status']}（{case['days_pending']}天）"
            )
    else:
        # 多通路：分組顯示
        for channel_name, cases in cases_by_channel.items():
            lines.append(f"\n【{channel_name}】{len(cases)} 筆")
            for i, case in enumerate(cases, start=1):
                prefix = _NUMBER_EMOJIS[i - 1] if i <= len(_NUMBER_EMOJIS) else f"({i})"
                lines.append(
                    f"{prefix} {case['case_id']} {case['name']} — {case['status']}（{case['days_pending']}天）"
                )

    lines.append(
        "\n⏰ 客戶熱度很重要～幫忙小助理趁早完成核准目標吧 🥳\n"
        "如果需要小助理幫忙協調或業務爭取的，快快跟我們說 別客氣😊"
    )

    return "\n".join(lines)


def reply_to_line(reply_token: str, message: str, channel_access_token: str) -> None:
    """透過 LINE Messaging API 回覆訊息。"""
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
            log_print(f"[line_oa_webhook] 回覆失敗，status={resp.status_code}, body={resp.text}")
    except Exception as e:
        log_print(f"[line_oa_webhook] reply_to_line 錯誤: {e}")
