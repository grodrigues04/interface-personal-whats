import base64
import json
from pathlib import Path
from typing import Any

import pytest

from interface_personal_whats import crypto, schemas
from interface_personal_whats.exceptions import InvalidPasswordError

VECTORS = json.loads((Path(__file__).parent.parent / "vectors" / "v1.json").read_text())


@pytest.fixture(scope="module")
def key() -> bytes:
    return crypto.derive_key(VECTORS["password"])


def encrypted_data(vector: dict[str, Any]) -> crypto.EncryptedData:
    return crypto.EncryptedData(
        nonce=base64.b64decode(vector["nonce_base64"]),
        ciphertext=base64.b64decode(vector["ciphertext_base64"]),
    )


def test_derive_key_matches_vectors(key: bytes) -> None:
    assert base64.b64encode(key).decode() == VECTORS["key_base64"]


@pytest.mark.parametrize("vector", VECTORS["messages"], ids=lambda vector: vector["description"])
def test_vectors_decrypt_to_valid_payloads(key: bytes, vector: dict[str, Any]) -> None:
    plaintext = crypto.decrypt(key, encrypted_data(vector))

    assert plaintext.decode() == vector["plaintext"]
    schemas.MessagePayload.model_validate_json(plaintext)


def test_vectors_key_check_is_valid(key: bytes) -> None:
    crypto.verify_key_check(key, encrypted_data(VECTORS["key_check"]))


def test_encrypt_uses_a_new_nonce_each_time(key: bytes) -> None:
    first = crypto.encrypt(key, b"oi")
    second = crypto.encrypt(key, b"oi")

    assert first.nonce != second.nonce
    assert crypto.decrypt(key, first) == crypto.decrypt(key, second) == b"oi"


def test_decrypt_with_wrong_password_raises_invalid_password(key: bytes) -> None:
    with pytest.raises(InvalidPasswordError):
        crypto.decrypt(crypto.derive_key("outra-senha"), crypto.encrypt(key, b"oi"))


def test_tampered_ciphertext_raises_invalid_password(key: bytes) -> None:
    encrypted = crypto.encrypt(key, b"oi")
    tampered = crypto.EncryptedData(
        nonce=encrypted.nonce,
        ciphertext=bytes([encrypted.ciphertext[0] ^ 1]) + encrypted.ciphertext[1:],
    )

    with pytest.raises(InvalidPasswordError):
        crypto.decrypt(key, tampered)


def test_derive_key_without_password_raises() -> None:
    with pytest.raises(ValueError, match="password"):
        crypto.derive_key("")
