import base64
import hashlib
from cryptography.fernet import Fernet
from src.infrastructure.logging import get_logger
from src.config import get_settings

logger = get_logger(__name__)


class SecretStorageError(RuntimeError):
    """Stored credentials could not be encrypted or decrypted safely."""


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
        except Exception as exc:
            logger.error("encryption_failed")
            raise SecretStorageError("Failed to encrypt stored credential") from exc

    def decrypt(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return self._cipher.decrypt(ciphertext.encode()).decode()
        except Exception as exc:
            logger.error("decryption_failed")
            raise SecretStorageError("Failed to decrypt stored credential") from exc
