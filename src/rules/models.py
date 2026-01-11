from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class RuleType(str, Enum):
    AUTOREAD = "autoread"
    AUTOREACT = "autoreact"


@dataclass
class Rule:
    id: Optional[int] = None
    user_id: int = 1
    rule_type: RuleType = RuleType.AUTOREAD
    chat_id: int = 0
    topic_id: Optional[int] = None  # None means applies to whole chat
    config: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class AutoReadRule(Rule):
    pass
