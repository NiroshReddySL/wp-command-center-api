"""Unit tests for the security module — crypto, passwords, tokens, SSRF guard."""
import pytest
from fastapi import HTTPException

from app.api.auth import (
    _SCOPES,
    ANALYTICS_SCOPE,
    SEARCH_CONSOLE_SCOPE,
    capabilities,
    missing_scopes,
)
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


class TestTokenForgery:
    """The classic JWT attacks, pinned against the library rather than assumed.

    These matter more than usual here: the signing library was swapped from
    python-jose to PyJWT (jose pulls in `ecdsa`, which carries a timing-attack
    advisory with no fix version). A swap that quietly relaxed verification
    would look exactly like a successful one — every existing test still
    passes, because valid tokens keep working.
    """

    def test_an_expired_token_is_rejected(self) -> None:
        from datetime import UTC, datetime, timedelta

        import jwt

        from app.config import settings
        expired = jwt.encode(
            {"sub": "u1", "role": "admin", "type": "access",
             "exp": datetime.now(UTC) - timedelta(seconds=1)},
            settings.SECRET_KEY, algorithm="HS256",
        )
        with pytest.raises(Exception):  # noqa: B017 - any rejection is correct
            _decode_token(expired)

    def test_a_token_signed_with_another_key_is_rejected(self) -> None:
        import jwt

        forged = jwt.encode(
            {"sub": "u1", "role": "admin", "type": "access"},
            "an-entirely-different-signing-key", algorithm="HS256",
        )
        with pytest.raises(Exception):  # noqa: B017
            _decode_token(forged)

    def test_the_none_algorithm_is_rejected(self) -> None:
        # The original JWT vulnerability: strip the signature, set alg to
        # "none", and an implementation that trusts the header waves it
        # through as an admin.
        import jwt

        unsigned = jwt.encode(
            {"sub": "u1", "role": "admin", "type": "access"}, key="", algorithm="none"
        )
        with pytest.raises(Exception):  # noqa: B017
            _decode_token(unsigned)

    def test_the_payload_cannot_be_edited_without_the_key(self) -> None:
        # Privilege escalation by rewriting the role claim.
        import base64
        import json

        header, payload, signature = create_access_token("u1", "member").split(".")
        raw = json.loads(base64.urlsafe_b64decode(payload + "=="))
        raw["role"] = "admin"
        tampered = base64.urlsafe_b64encode(
            json.dumps(raw).encode()
        ).decode().rstrip("=")
        with pytest.raises(Exception):  # noqa: B017
            _decode_token(f"{header}.{tampered}.{signature}")


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


class TestGoogleScopeVerification:
    """Google returns a perfectly valid token even when the sensitive scopes
    were declined — its consent screen lets them be approved individually,
    and a Cloud project that hasn't configured them gets the same result.
    Storing that and reporting "connected" is how every Analytics call ends
    up returning ACCESS_TOKEN_SCOPE_INSUFFICIENT into a log file behind a
    green tick.
    """

    FULL = (
        "https://www.googleapis.com/auth/analytics.readonly "
        "https://www.googleapis.com/auth/webmasters.readonly openid email"
    )

    def test_a_full_grant_has_nothing_missing(self) -> None:
        assert missing_scopes(self.FULL) == []
        assert capabilities(self.FULL) == {"analytics": True, "search_console": True}

    def test_an_identity_only_grant_is_detected(self) -> None:
        # Exactly what was observed live: sign-in worked, every data call 403'd.
        granted = "https://www.googleapis.com/auth/userinfo.email openid"
        assert capabilities(granted) == {"analytics": False, "search_console": False}
        assert len(missing_scopes(granted)) == 2

    def test_a_partial_grant_names_only_what_is_absent(self) -> None:
        granted = "https://www.googleapis.com/auth/analytics.readonly openid"
        assert capabilities(granted) == {"analytics": True, "search_console": False}
        assert missing_scopes(granted) == [SEARCH_CONSOLE_SCOPE]

    def test_no_scope_string_is_not_treated_as_full_access(self) -> None:
        for empty in (None, ""):
            assert capabilities(empty) == {"analytics": False, "search_console": False}
            assert len(missing_scopes(empty)) == 2

    def test_required_scopes_are_actually_requested(self) -> None:
        # The check is worthless if the authorize URL never asks for them.
        assert ANALYTICS_SCOPE in _SCOPES
        assert SEARCH_CONSOLE_SCOPE in _SCOPES
