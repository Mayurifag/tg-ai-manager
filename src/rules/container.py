from src.rules.sqlite_repo import SqliteRuleRepository
from src.rules.service import RuleService
from src.adapters.sqlite_repo import SqliteActionRepository

_rule_service_instance: RuleService | None = None

def get_rule_service() -> RuleService:
    global _rule_service_instance
    if _rule_service_instance is None:
        rule_repo = SqliteRuleRepository()
        action_repo = SqliteActionRepository()  # shared with main interactor
        _rule_service_instance = RuleService(rule_repo, action_repo)
    return _rule_service_instance