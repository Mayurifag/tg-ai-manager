from typing import Optional
from src.ai.ports import AIProvider
from src.ai.providers.gemini import GeminiProvider
from src.ai.providers.openai_compatible import OpenAICompatibleProvider
from src.settings.models import GlobalSettings
from src.infrastructure.security import CryptoManager


def create_ai_provider(settings: GlobalSettings) -> Optional[AIProvider]:
    """Factory to create the configured AI provider."""
    if not settings.ai_enabled or not settings.ai_api_key:
        return None

    crypto = CryptoManager()
    api_key = crypto.decrypt(settings.ai_api_key)

    if not api_key:
        return None

    if settings.ai_provider == "gemini":
        return GeminiProvider(api_key=api_key, model=settings.ai_model or "gemini-pro")

    elif settings.ai_provider == "openai":
        base_url = settings.ai_base_url or "https://api.openai.com/v1"
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=settings.ai_model or "gpt-3.5-turbo",
        )

    elif settings.ai_provider == "local":
        # Local usually mimics OpenAI
        base_url = settings.ai_base_url or "http://localhost:11434/v1"
        return OpenAICompatibleProvider(
            api_key="sk-local",  # Usually ignored
            base_url=base_url,
            model=settings.ai_model or "llama2",
        )

    return None
