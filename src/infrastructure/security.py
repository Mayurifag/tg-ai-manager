import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from src.infrastructure.logging import get_logger
from src.config import get_settings

logger = get_logger(__name__)


class CryptoManager:
    def __init__(self):
        self._key = self._derive_key()
        self._cipher = Fernet(self._key)

    def _derive_key(self) -> bytes:
        settings = get_settings()
        if not settings.TG_API_HASH:
            raise ValueError("TG_API_HASH must be set for encryption.")

        digest = hashlib.sha256(settings.TG_API_HASH.encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, plaintext: str | None) -> str | None:
        if not plaintext:
            return None
        try:
            return self._cipher.encrypt(plaintext.encode()).decode()
        except Exception as e:
            logger.error("encryption_failed", error=str(e))
            return plaintext

    def decrypt(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return self._cipher.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            logger.warning("decryption_failed_invalid_token")
            return None
        except Exception as e:
            logger.error("decryption_failed", error=str(e))
            return None
