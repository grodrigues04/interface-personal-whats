"""AES-256-GCM encryption shared by every project of the personalWhats queue.

Any change here must also be made in the JavaScript implementations (personalWhats/whatsapp
and personal-whats-front) and checked against tests/vectors/v1.json.
"""

import hashlib
import os
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import InvalidPasswordError

ENCRYPTION_VERSION = 1
KEY_DERIVATION_SALT = b"personal-whats/v1"
KEY_DERIVATION_ITERATIONS = 600_000
KEY_SIZE = 32
NONCE_SIZE = 12

KEY_CHECK_PLAINTEXT = b"personal-whats-key-check"


@dataclass(frozen=True)
class EncryptedData:
    nonce: bytes
    ciphertext: bytes  # ciphertext followed by the 16 byte GCM tag


def derive_key(password: str) -> bytes:
    """PBKDF2-HMAC-SHA256 with a fixed salt, so the key is derived once per process."""
    if not password:
        raise ValueError("encryption password is required")  # noqa: TRY003
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), KEY_DERIVATION_SALT, KEY_DERIVATION_ITERATIONS, KEY_SIZE
    )


def encrypt(key: bytes, plaintext: bytes, nonce: Optional[bytes] = None) -> EncryptedData:
    """`nonce` exists only for the test vectors: real messages always get a random one."""
    nonce = nonce if nonce is not None else os.urandom(NONCE_SIZE)
    return EncryptedData(nonce=nonce, ciphertext=AESGCM(key).encrypt(nonce, plaintext, None))


def decrypt(key: bytes, encrypted_data: EncryptedData) -> bytes:
    """Raises InvalidPasswordError when the key is wrong or the data was tampered with."""
    try:
        return AESGCM(key).decrypt(encrypted_data.nonce, encrypted_data.ciphertext, None)
    except InvalidTag as error:
        raise InvalidPasswordError() from error


def build_key_check(key: bytes) -> EncryptedData:
    return encrypt(key, KEY_CHECK_PLAINTEXT)


def verify_key_check(key: bytes, key_check: EncryptedData) -> None:
    if decrypt(key, key_check) != KEY_CHECK_PLAINTEXT:
        raise InvalidPasswordError()
