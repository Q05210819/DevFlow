import traceback

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from devflow.errors import AppError


def test_ciphertext_and_logs_hide_tokens(workspace_tmp_path, caplog):
    from devflow.security import CredentialCipher

    key_file = workspace_tmp_path / "fernet-key"
    key_file.write_bytes(Fernet.generate_key() + b"\n")
    cipher = CredentialCipher.from_file(key_file)
    secret = SecretStr("token-sentinel")
    ciphertext = cipher.encrypt(secret)
    assert isinstance(ciphertext, bytes)
    assert b"token-sentinel" not in ciphertext
    assert cipher.encrypt(secret) != ciphertext
    assert cipher.decrypt(ciphertext).get_secret_value() == "token-sentinel"
    assert (
        "token-sentinel"
        not in repr(cipher) + repr(cipher.decrypt(ciphertext)) + caplog.text
    )


@pytest.mark.parametrize(
    "contents", [b"private-sentinel", b"", b"\xffprivate-sentinel"]
)
def test_invalid_key_is_controlled(workspace_tmp_path, contents):
    from devflow.security import CredentialCipher

    key_file = workspace_tmp_path / "key"
    key_file.write_bytes(contents)
    with pytest.raises(AppError) as caught:
        CredentialCipher.from_file(key_file)
    assert caught.value.code == "github_configuration_error"
    assert "private-sentinel" not in "".join(traceback.format_exception(caught.value))


def test_missing_key_is_controlled(workspace_tmp_path):
    from devflow.security import CredentialCipher

    missing_path = workspace_tmp_path / "private-sentinel"
    with pytest.raises(AppError) as caught:
        CredentialCipher.from_file(missing_path)
    assert caught.value.code == "github_configuration_error"
    assert "private-sentinel" not in "".join(traceback.format_exception(caught.value))


def test_wrong_key_or_tampering_cannot_decrypt():
    from devflow.security import CredentialCipher

    first = CredentialCipher(Fernet.generate_key())
    second = CredentialCipher(Fernet.generate_key())
    encrypted = first.encrypt(SecretStr("private-sentinel"))
    for ciphertext in (encrypted, b"private-sentinel"):
        with pytest.raises(AppError) as caught:
            second.decrypt(ciphertext)
        assert caught.value.code == "github_configuration_error"
        assert "private-sentinel" not in "".join(
            traceback.format_exception(caught.value)
        )
