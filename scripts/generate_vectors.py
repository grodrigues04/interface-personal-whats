"""Generates tests/vectors/v1.json, used to check the JavaScript implementations.

Run: uv run python scripts/generate_vectors.py
"""

import base64
import json
from pathlib import Path

from interface_personal_whats import crypto, schemas

PASSWORD = "senha-de-teste"
VECTORS_FILE = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "v1.json"


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def main() -> None:
    key = crypto.derive_key(PASSWORD)
    payloads = {
        "text only": schemas.MessagePayload(text="Olá! Mensagem de teste ✅"),
        "text, recipient and attachment": schemas.MessagePayload(
            text="Relatório da semana",
            recipient="5548999998888",
            attachments=[
                schemas.Attachment(
                    file_name="relatorio.pdf",
                    mimetype="application/pdf",
                    content=b"%PDF-1.4 conteudo de teste",
                    caption="Resumo: 3 vagas novas",
                )
            ],
        ),
        "group recipient": schemas.MessagePayload(
            text="Mensagem para grupo", recipient="120363427990271477@g.us"
        ),
    }
    messages = []
    for index, (description, payload) in enumerate(payloads.items()):
        plaintext = payload.model_dump_json().encode("utf-8")
        encrypted_data = crypto.encrypt(
            key, plaintext, nonce=bytes([index + 1]) * crypto.NONCE_SIZE
        )
        messages.append(
            {
                "description": description,
                "nonce_base64": to_base64(encrypted_data.nonce),
                "ciphertext_base64": to_base64(encrypted_data.ciphertext),
                "plaintext": plaintext.decode("utf-8"),
            }
        )
    key_check = crypto.encrypt(key, crypto.KEY_CHECK_PLAINTEXT, nonce=bytes(crypto.NONCE_SIZE))
    vectors = {
        "encryption_version": crypto.ENCRYPTION_VERSION,
        "password": PASSWORD,
        "key_derivation": {
            "algorithm": "PBKDF2-HMAC-SHA256",
            "salt_utf8": crypto.KEY_DERIVATION_SALT.decode("utf-8"),
            "iterations": crypto.KEY_DERIVATION_ITERATIONS,
            "key_size": crypto.KEY_SIZE,
        },
        "key_base64": to_base64(key),
        "key_check": {
            "plaintext_utf8": crypto.KEY_CHECK_PLAINTEXT.decode("utf-8"),
            "nonce_base64": to_base64(key_check.nonce),
            "ciphertext_base64": to_base64(key_check.ciphertext),
        },
        "messages": messages,
    }
    VECTORS_FILE.write_text(json.dumps(vectors, indent=2, ensure_ascii=False) + "\n")
    print(f"Vectors written to {VECTORS_FILE}")


if __name__ == "__main__":
    main()
