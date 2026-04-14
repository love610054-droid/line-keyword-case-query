from datetime import datetime


def log_print(message: str) -> None:
    """最小版 logger：輸出時間戳 + 訊息。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}")
