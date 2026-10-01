"""Read mounted secrets and encrypt stored credentials without exposing inputs."""

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from devflow.errors import AppError


def secret_configuration_error() -> AppError:
    return AppError(
        "github_configuration_error", 503, "Credential configuration unavailable"
    )


def read_secret_file(path: Path | None) -> bytes:
    try:
        if path is None:
            raise ValueError
        value = path.read_bytes().strip()
        if not value:
            raise ValueError
        return value
    except (OSError, ValueError):
        raise secret_configuration_error() from None


class CredentialCipher:
    """Fernet key is 32 random bytes encoded as URL-safe base64."""

    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise secret_configuration_error() from None

    @classmethod
    def from_file(cls, path: Path) -> "CredentialCipher":
        return cls(read_secret_file(path))

    def encrypt(self, value: SecretStr) -> bytes:
        return self._fernet.encrypt(value.get_secret_value().encode("utf-8"))

    def decrypt(self, value: bytes) -> SecretStr:
        try:
            return SecretStr(self._fernet.decrypt(value).decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, TypeError):
            raise secret_configuration_error() from None


def random_token() -> SecretStr:
    """256 bits of entropy; URL-safe without padding."""
    import secrets

    return SecretStr(secrets.token_urlsafe(32))


def token_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token(signing_key: bytes, raw_session: str) -> str:
    import hashlib
    import hmac

    if len(signing_key) < 32:
        raise secret_configuration_error()
    return hmac.digest(
        signing_key, b"devflow:csrf:v1\0" + raw_session.encode("utf-8"), hashlib.sha256
    ).hex()


def verify_csrf(signing_key: bytes, raw_session: str, supplied: str) -> bool:
    import hmac

    return hmac.compare_digest(
        csrf_token(signing_key, raw_session).encode("ascii"), supplied.encode("utf-8")
    )
