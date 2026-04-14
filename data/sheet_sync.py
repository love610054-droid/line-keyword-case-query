import os
from typing import Optional

from data.logger import log_print


class SheetSync:
    """Google Sheets 讀取器（案件主表）。"""

    def __init__(self) -> None:
        self._service = None

    def _sheet_id(self) -> str:
        return os.environ.get("GOOGLE_SHEET_ID", "").strip()

    def _tab_name(self) -> str:
        # 若未指定，預設使用第一個案件工作表名稱
        return os.environ.get("SHEET_TAB_CASES", "工作表1").strip() or "工作表1"

    def _creds_path(self) -> str:
        # 相容兩種命名：GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_SA_KEY_PATH
        return (
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            or os.environ.get("GOOGLE_SA_KEY_PATH", "").strip()
        )

    def _get_service(self):
        if self._service is not None:
            return self._service

        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_path = self._creds_path()
        if not creds_path:
            raise RuntimeError("缺少 Google 憑證路徑（GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_SA_KEY_PATH）")

        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _range_all(self) -> str:
        tab = self._tab_name().replace("'", "''")
        # 讀 A:I，對應 日期、通路、案件編號、姓名、商品、狀態、核准日期、備註、原始審核結果
        return f"'{tab}'!A:I"

    def _get_all_rows(self) -> list[list[str]]:
        """回傳案件列資料（不含表頭）。"""
        sheet_id = self._sheet_id()
        if not sheet_id:
            log_print("[sheet_sync] 缺少 GOOGLE_SHEET_ID，回傳空資料")
            return []

        try:
            service = self._get_service()
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=self._range_all())
                .execute()
            )
            rows = result.get("values", [])
            if not rows:
                return []

            # 若第一列是表頭，略過
            if rows and len(rows[0]) >= 6:
                header_a = str(rows[0][0]).strip()
                header_f = str(rows[0][5]).strip() if len(rows[0]) > 5 else ""
                if header_a == "日期" and header_f == "狀態":
                    return rows[1:]

            return rows
        except Exception as e:
            log_print(f"[sheet_sync] 讀取 Google Sheet 失敗: {e}")
            return []


sheet_sync = SheetSync()
