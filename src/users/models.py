from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    id: int = 1
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    username: Optional[str] = None
    session_string: Optional[str] = None
    is_premium: bool = False

    # Settings (Strict Booleans)
    autoread_service_messages: bool = False
    autoread_polls: bool = False
    autoread_self: bool = False

    # Debug
    debug_mode: bool = False

    autoread_bots: str = "@lolsBotCatcherBot"
    autoread_regex: str = ""

    def is_authenticated(self) -> bool:
        return bool(self.session_string and self.api_id and self.api_hash)
