"""群組 ID ↔ 通路名稱對照：優先讀寫 Google Sheet，未設定憑證時改用本機 JSON。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Optional

from data.logger import log_print

# 與主案件表可為同一個 Spreadsheet，不同工作表名稱
_DEFAULT_TAB = "群組通路對照"
_LOCAL_FILE = Path(__file__).resolve().parent / "group_mapping_local.json"


class GroupMappingSync:
    def __init__(self) -> None:
        self._lock = Lock()
        self._memory: dict[str, str] = {}
        self._loaded = False

    def _sheet_id(self) -> str:
        return os.environ.get("GOOGLE_SHEET_ID", "").strip()

    def _tab_name(self) -> str:
        return os.environ.get("SHEET_TAB_GROUP_MAPPING", _DEFAULT_TAB).strip() or _DEFAULT_TAB

    def _creds_path(self) -> str:
        return os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    def _use_google(self) -> bool:
        return bool(self._sheet_id() and self._creds_path() and Path(self._creds_path()).is_file())

    def _load_local_file(self) -> dict[str, str]:
        if not _LOCAL_FILE.is_file():
            return {}
        try:
            raw = json.loads(_LOCAL_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "mappings" in raw:
                out: dict[str, str] = {}
                for item in raw.get("mappings", []):
                    if not isinstance(item, dict):
                        continue
                    gid = str(item.get("group_id", "")).strip()
                    ch = str(item.get("channel_name", "")).strip()
                    if gid and ch:
                        out[gid] = ch
                return out
        except Exception as e:
            log_print(f"[group_mapping_sync] 讀取本機對照檔失敗: {e}")
        return {}

    def _save_local_file(self, group_id: str, group_name: str, channel_name: str) -> None:
        try:
            _LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {"mappings": []}
            if _LOCAL_FILE.is_file():
                try:
                    data = json.loads(_LOCAL_FILE.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {"mappings": []}
            if "mappings" not in data or not isinstance(data["mappings"], list):
                data["mappings"] = []
            found = False
            for item in data["mappings"]:
                if isinstance(item, dict) and str(item.get("group_id", "")).strip() == group_id:
                    item["group_name"] = group_name
                    item["channel_name"] = channel_name
                    found = True
                    break
            if not found:
                data["mappings"].append(
                    {
                        "group_id": group_id,
                        "group_name": group_name,
                        "channel_name": channel_name,
                    }
                )
            _LOCAL_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log_print(f"[group_mapping_sync] 寫入本機對照檔失敗: {e}")

    def _google_service(self):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(self._creds_path(), scopes=scopes)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    def _range_all(self) -> str:
        tab = self._tab_name()
        safe = tab.replace("'", "''")
        return f"'{safe}'!A:C"

    def _reload_from_backend(self) -> None:
        self._memory.clear()
        if self._use_google():
            try:
                svc = self._google_service()
                sid = self._sheet_id()
                rng = self._range_all()
                result = (
                    svc.spreadsheets()
                    .values()
                    .get(spreadsheetId=sid, range=rng)
                    .execute()
                )
                rows = result.get("values", [])
                for row in rows[1:]:  # 略過表頭
                    if len(row) < 3:
                        continue
                    gid = str(row[0]).strip()
                    ch = str(row[2]).strip()
                    if gid and ch:
                        self._memory[gid] = ch
            except ImportError as e:
                log_print(
                    f"[group_mapping_sync] 未安裝 Google API 套件，改用本機對照檔: {e}"
                )
                self._memory.update(self._load_local_file())
            except Exception as e:
                log_print(f"[group_mapping_sync] 從 Google Sheet 載入對照失敗: {e}")
                self._memory.update(self._load_local_file())
        else:
            self._memory.update(self._load_local_file())
        self._loaded = True

    def get_channel_by_group_id(self, group_id: str) -> Optional[str]:
        with self._lock:
            if not self._loaded:
                self._reload_from_backend()
            return self._memory.get(group_id)

    def upsert_mapping(self, group_id: str, group_name: str, channel_name: str) -> None:
        if not group_id or not channel_name:
            return
        with self._lock:
            self._memory[group_id] = channel_name
            self._loaded = True
            if self._use_google():
                try:
                    svc = self._google_service()
                    sid = self._sheet_id()
                    rng = self._range_all()
                    result = (
                        svc.spreadsheets()
                        .values()
                        .get(spreadsheetId=sid, range=rng)
                        .execute()
                    )
                    rows = result.get("values", [])
                    body = [[group_id, group_name, channel_name]]
                    # 列 1 表頭；資料從列 2 起
                    row_idx = None
                    for i, row in enumerate(rows[1:], start=2):
                        if row and str(row[0]).strip() == group_id:
                            row_idx = i
                            break
                    if row_idx is not None:
                        tab = self._tab_name()
                        safe = tab.replace("'", "''")
                        update_range = f"'{safe}'!A{row_idx}:C{row_idx}"
                        svc.spreadsheets().values().update(
                            spreadsheetId=sid,
                            range=update_range,
                            valueInputOption="USER_ENTERED",
                            body={"values": body},
                        ).execute()
                        log_print(
                            f"[group_mapping_sync] 已更新 Sheet 對照列：{group_id} → {channel_name}"
                        )
                    else:
                        svc.spreadsheets().values().append(
                            spreadsheetId=sid,
                            range=rng,
                            valueInputOption="USER_ENTERED",
                            insertDataOption="INSERT_ROWS",
                            body={"values": body},
                        ).execute()
                        log_print(
                            f"[group_mapping_sync] 已新增 Sheet 對照列：{group_id} → {channel_name}"
                        )
                except Exception as e:
                    log_print(f"[group_mapping_sync] 寫入 Google Sheet 失敗，改寫本機檔: {e}")
                    self._save_local_file(group_id, group_name, channel_name)
            else:
                self._save_local_file(group_id, group_name, channel_name)


group_mapping_sync = GroupMappingSync()
