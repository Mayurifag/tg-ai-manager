from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from datetime import datetime


class RuleType(str, Enum):
    AUTOREAD = "autoread"


@dataclass
class Rule:
    id: Optional[int] = None
    user_id: int = 1
    rule_type: RuleType = RuleType.AUTOREAD
    chat_id: int = 0
    topic_id: Optional[int] = None  # None means applies to whole chat
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class AutoReadRule(Rule):
    pass
