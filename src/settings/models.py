from dataclasses import dataclass
from typing import Optional


@dataclass
class GlobalSettings:
    id: int = 1
    autoread_service_messages: bool = False
    autoread_polls: bool = False
    autoread_bots: str = "@lolsBotCatcherBot"
    autoread_regex: str = ""
    autoread_self: bool = False

    # AI Settings
    ai_enabled: bool = False
    ai_provider: str = "gemini"  # gemini, openai, local
    ai_model: str = "gemini-pro"
    ai_api_key: Optional[str] = None  # Encrypted
    ai_base_url: Optional[str] = None

    # AI Features
    skip_ads_enabled: bool = False
