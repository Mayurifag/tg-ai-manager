from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReadDecision:
    should_read: bool
    reason: str = ""
    max_id: Optional[int] = None


@dataclass(frozen=True)
class ReactDecision:
    should_react: bool
    emoji: str = ""
