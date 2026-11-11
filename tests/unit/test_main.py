from typing import Any

import mongomock
import pytest
from bson import ObjectId
from pydantic import ValidationError

from interface_personal_whats import (
    InterfacePersonalWhats,
    InvalidPasswordError,
    MessageNotFoundError,
    MessageStatus,
    PersonalWhatsNotInitializedError,
    crypto,
    main,
    schemas,
)

PASSWORD = "senha-de-teste"


@pytest.fixture(scope="module")
def key() -> bytes:
    return crypto.derive_key(PASSWORD)


@pytest.fixture
def mongo_client() -> Any:
    return mongomock.MongoClient(tz_aware=True)


@pytest.fixture
def initialized_mongo(mongo_client: Any, key: bytes) -> Any:
    """Writes the key check, like the validator does on startup."""
    key_check = crypto.build_key_check(key)
    mongo_client[main.DEFAULT_DATABASE_NAME][main.SETTINGS_COLLECTION].insert_one(
        {"_id": main.KEY_CHECK_ID, "nonce": key_check.nonce, "ciphertext": key_check.ciphertext}
    )
    return mongo_client


@pytest.fixture(autouse=True)
def fast_key_derivation(monkeypatch: pytest.MonkeyPatch, key: bytes) -> None:
    monkeypatch.setattr(
        crypto, "derive_key", lambda password: key if password == PASSWORD else b"x" * 32
    )


def interface(mongo_client: Any, password: str = PASSWORD) -> InterfacePersonalWhats:
    return InterfacePersonalWhats("mongodb://unused", password, mongo_client=mongo_client)


def messages(mongo_client: Any) -> Any:
    return mongo_client[main.DEFAULT_DATABASE_NAME][main.MESSAGES_COLLECTION]


def test_send_message_stores_encrypted_payload(initialized_mongo: Any, key: bytes) -> None:
    message_id = interface(initialized_mongo).send_message(
        "vagas_automator", text="Relatório", recipient="+55 48 99999-8888"
    )

    document = messages(initialized_mongo).find_one({"_id": ObjectId(message_id)})
    assert document["source"] == "vagas_automator"
    assert document["status"] == MessageStatus.RECEIVED.value
    assert document["encryption_version"] == crypto.ENCRYPTION_VERSION
    assert b"Relat" not in bytes(document["ciphertext"])
    payload = main.decrypt_payload(key, document)
    assert (payload.text, payload.recipient) == ("Relatório", "5548999998888")


def test_send_message_with_wrong_password_queues_nothing(initialized_mongo: Any) -> None:
    with pytest.raises(InvalidPasswordError):
        interface(initialized_mongo, password="errada").send_message("vagas", text="oi")

    assert messages(initialized_mongo).count_documents({}) == 0


def test_send_message_before_validator_started_raises(mongo_client: Any) -> None:
    with pytest.raises(PersonalWhatsNotInitializedError):
        interface(mongo_client).send_message("vagas", text="oi")


def test_send_invalid_message_queues_nothing(initialized_mongo: Any) -> None:
    with pytest.raises(ValidationError):
        interface(initialized_mongo).send_message("vagas")
    with pytest.raises(ValidationError):
        interface(initialized_mongo).send_message("Fonte Inválida", text="oi")

    assert messages(initialized_mongo).count_documents({}) == 0


def test_send_message_with_attachment(initialized_mongo: Any, key: bytes) -> None:
    attachment = schemas.Attachment(
        file_name="relatorio.pdf", mimetype="application/pdf", content=b"%PDF", caption="Resumo"
    )

    message_id = interface(initialized_mongo).send_message("vagas", attachments=[attachment])

    document = messages(initialized_mongo).find_one({"_id": ObjectId(message_id)})
    assert main.decrypt_payload(key, document).attachments == [attachment]


def test_get_message_returns_public_fields(initialized_mongo: Any) -> None:
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="oi")

    info = client.get_message(message_id)

    assert (info.id, info.source, info.status, info.attempts) == (
        message_id,
        "vagas",
        MessageStatus.RECEIVED,
        0,
    )


@pytest.mark.parametrize("message_id", ["nao-e-um-id", str(ObjectId())])
def test_get_unknown_message_raises(initialized_mongo: Any, message_id: str) -> None:
    with pytest.raises(MessageNotFoundError):
        interface(initialized_mongo).get_message(message_id)
