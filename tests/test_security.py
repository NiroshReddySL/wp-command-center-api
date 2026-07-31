"""Unit tests for the security module — crypto, passwords, tokens, SSRF guard."""
import pytest
from fastapi import HTTPException

from app.security.auth import (
    _decode_token,
    create_access_token,
    create_state_token,
    hash_password,
    verify_password,
    verify_state_token,
)
from app.security.crypto import decrypt_value, encrypt_value
from app.security.url_guard import _is_public_address, ensure_public_url


class TestCrypto:
    def test_round_trip(self) -> None:
        secret = "wp-application-password-123"
        encrypted = encrypt_value(secret)
        assert encrypted != secret
        assert decrypt_value(encrypted) == secret

    def test_legacy_plaintext_passthrough(self) -> None:
        # Rows written before encryption existed must still read correctly
        assert decrypt_value("legacy-plaintext-key") == "legacy-plaintext-key"

    def test_unique_ciphertexts(self) -> None:
        # Fernet includes a random IV — same input must not produce same output
        assert encrypt_value("same") != encrypt_value("same")


class TestPasswords:
    def test_verify_correct_password(self) -> None:
        h = hash_password("s3cret-password")
        assert verify_password("s3cret-password", h)

    def test_reject_wrong_password(self) -> None:
        h = hash_password("s3cret-password")
        assert not verify_password("wrong", h)

    def test_reject_malformed_hash(self) -> None:
        assert not verify_password("anything", "not-a-bcrypt-hash")


class TestTokens:
    def test_access_token_round_trip(self) -> None:
        token = create_access_token("user-123", "admin")
        payload = _decode_token(token)
        assert payload["sub"] == "user-123"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"

    def test_state_token_verifies(self) -> None:
        assert verify_state_token(create_state_token())

    def test_tampered_state_rejected(self) -> None:
        assert not verify_state_token("tampered." + create_state_token())

    def test_access_token_is_not_valid_state(self) -> None:
        # Token-type confusion must not be possible
        assert not verify_state_token(create_access_token("user-123", "admin"))


class TestUrlGuard:
    def test_public_and_private_addresses(self) -> None:
        assert _is_public_address("93.184.216.34")
        assert not _is_public_address("127.0.0.1")
        assert not _is_public_address("10.0.0.5")
        assert not _is_public_address("192.168.1.1")
        assert not _is_public_address("169.254.169.254")  # cloud metadata

    @pytest.mark.asyncio
    async def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await ensure_public_url("ftp://example.com")
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_loopback_url(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await ensure_public_url("http://127.0.0.1:8000")
        assert exc.value.status_code == 422
