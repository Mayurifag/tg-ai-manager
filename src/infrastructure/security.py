import os
from cryptography.fernet import Fernet, InvalidToken
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

KEY_FILE = "secret.key"


class CryptoManager:
    def __init__(self):
        self._key = self._load_or_generate_key()
        self._cipher = Fernet(self._key)

    def _load_or_generate_key(self) -> bytes:
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, "rb") as f:
                return f.read()
        else:
            logger.info("generating_new_encryption_key")
            key = Fernet.generate_key()
            with open(KEY_FILE, "wb") as f:
                f.write(key)
            return key

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
            # Fallback for legacy plain text data
            logger.warning("decryption_failed_invalid_token_returning_raw")
            return ciphertext
        except Exception as e:
            logger.error("decryption_failed", error=str(e))
            return ciphertext
