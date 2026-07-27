"""Secrets-at-rest encryption for third-party credentials.

WordPress application passwords and Google OAuth tokens are encrypted with
Fernet before hitting the database. The `EncryptedText` column type is a
drop-in replacement for `Text`: it encrypts on write and decrypts on read,
falling back to plaintext for legacy rows written before encryption existed
(they are re-encrypted the next time they're saved).
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import settings

logger = logging.getLogger(__name__)


def _build_fernet() -> Fernet:
    if settings.TOKEN_ENCRYPTION_KEY:
        return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    # Derive a stable key from SECRET_KEY so dev works out of the box.
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


_fernet = _build_fernet()


def encrypt_value(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    """Decrypt a stored value; return it unchanged if it's a legacy plaintext row."""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return value


class EncryptedText(TypeDecorator[str]):
    """Text column encrypted at rest. Same DB type as Text — no migration needed."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        if value is None or value == "":
            return value
        return encrypt_value(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        if value is None or value == "":
            return value
        return decrypt_value(value)
