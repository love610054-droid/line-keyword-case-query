class RuleManager:
    """最小版規則管理器。

    回傳格式需與 line_oa_webhook 相容：
    [{"channel_name": "...", "condition_keywords": ["關鍵字"]}, ...]
    """

    def __init__(self) -> None:
        self._rules: list[dict] = []

    def get_rules(self) -> list[dict]:
        return self._rules

    def set_rules(self, rules: list[dict]) -> None:
        self._rules = rules


rule_manager = RuleManager()
