from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from datetime import datetime


class RuleType(str, Enum):
    AUTOREAD = "autoread"
    # Future: SKIP, DELETE, REPLY, etc.


@dataclass
class Rule:
    id: Optional[int] = None
    rule_type: RuleType = RuleType.AUTOREAD
    chat_id: int = 0
    topic_id: Optional[int] = None  # None means applies to whole chat
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class AutoReadRule(Rule):
    pass  # Currently no extra fields, but ready for future extensions
