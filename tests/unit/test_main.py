from datetime import datetime, timedelta, timezone
from typing import Any

import mongomock
import pytest
from bson import ObjectId
from pydantic import ValidationError

from interface_personal_whats import (
    REPLY_WINDOW,
    InterfacePersonalWhats,
    InvalidPasswordError,
    MessageNotFoundError,
    MessageNotSentError,
    MessageStatus,
    PersonalWhatsNotInitializedError,
    Reply,
    ReplyNotExpectedError,
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


def store_reply(mongo_client: Any, key: bytes, message_id: str, text: str) -> None:
    """Writes a reply like personalWhats/whatsapp does."""
    encrypted_reply = crypto.encrypt(
        key,
        schemas.ReplyPayload(text=text, message_type="conversation")
        .model_dump_json()
        .encode("utf-8"),
    )
    messages(mongo_client).update_one(
        {"_id": ObjectId(message_id)},
        {
            "$set": {
                "status": MessageStatus.SENT.value,
                "reply": {"nonce": encrypted_reply.nonce, "ciphertext": encrypted_reply.ciphertext},
                "replied_at": datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
            }
        },
    )


def mark_sent(mongo_client: Any, message_id: str, sent_at: datetime) -> None:
    messages(mongo_client).update_one(
        {"_id": ObjectId(message_id)},
        {
            "$set": {
                "status": MessageStatus.SENT.value,
                "sent_at": sent_at,
                "reply_deadline": sent_at + REPLY_WINDOW,
            }
        },
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr("interface_personal_whats.main.time.sleep", sleeps.append)
    return sleeps


def test_send_message_expecting_reply(initialized_mongo: Any, key: bytes) -> None:
    client = interface(initialized_mongo)

    expecting_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)
    plain_id = client.send_message("vagas", text="oi")

    assert (
        main.decrypt_payload(
            key, messages(initialized_mongo).find_one({"_id": ObjectId(expecting_id)})
        ).text
        == "(RESPOSTA ÚNICA) Confirma?"
    )
    assert client.get_message(expecting_id).expect_unique_reply is True
    assert client.get_message(plain_id).expect_unique_reply is False
    assert client.get_reply(expecting_id) is None
    with pytest.raises(ReplyNotExpectedError):
        client.get_reply(plain_id)
    with pytest.raises(ReplyNotExpectedError):
        client.wait_for_reply(plain_id)


def test_wait_for_reply_returns_the_decrypted_reply(
    initialized_mongo: Any, key: bytes, sleeps: list[float]
) -> None:
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)
    store_reply(initialized_mongo, key, message_id, "sim")

    reply = client.wait_for_reply(message_id)

    assert reply == Reply(
        text="sim",
        message_type="conversation",
        received_at=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
    )
    assert sleeps == []
    info = client.get_message(message_id)
    assert info.replied_at == reply.received_at


def test_wait_for_reply_polls_until_the_reply_arrives(
    initialized_mongo: Any, key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)
    mark_sent(initialized_mongo, message_id, datetime.now(timezone.utc))
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            store_reply(initialized_mongo, key, message_id, "não")

    monkeypatch.setattr("interface_personal_whats.main.time.sleep", sleep)

    reply = client.wait_for_reply(message_id, poll_interval_seconds=3)

    assert reply is not None and reply.text == "não"
    assert sleeps == [3, 3]


def test_wait_for_reply_returns_none_after_the_reply_window(
    initialized_mongo: Any, sleeps: list[float]
) -> None:
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)
    mark_sent(initialized_mongo, message_id, datetime.now(timezone.utc) - timedelta(minutes=11))

    assert client.wait_for_reply(message_id) is None
    assert sleeps == []


def test_wait_for_reply_counts_the_window_from_sent_at_without_deadline(
    initialized_mongo: Any, sleeps: list[float]
) -> None:
    """Versions of personalWhats without replies never write reply_deadline."""
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)
    messages(initialized_mongo).update_one(
        {"_id": ObjectId(message_id)},
        {
            "$set": {
                "status": MessageStatus.SENT.value,
                "sent_at": datetime.now(timezone.utc) - timedelta(minutes=11),
            }
        },
    )

    assert client.wait_for_reply(message_id) is None


@pytest.mark.parametrize("status", [MessageStatus.REJECTED, MessageStatus.FAILED])
def test_wait_for_reply_raises_when_the_message_was_not_sent(
    initialized_mongo: Any, status: MessageStatus, sleeps: list[float]
) -> None:
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)
    messages(initialized_mongo).update_one(
        {"_id": ObjectId(message_id)}, {"$set": {"status": status.value, "error": "motivo"}}
    )

    with pytest.raises(MessageNotSentError, match="motivo"):
        client.wait_for_reply(message_id)


def test_wait_for_reply_raises_when_sending_takes_too_long(
    initialized_mongo: Any, sleeps: list[float]
) -> None:
    client = interface(initialized_mongo)
    message_id = client.send_message("vagas", text="Confirma?", expect_unique_reply=True)

    with pytest.raises(MessageNotSentError, match="received"):
        client.wait_for_reply(message_id, send_timeout=timedelta(seconds=0))


@pytest.mark.parametrize(
    ("text", "expected_text"),
    [(None, "(RESPOSTA ÚNICA)"), ("  ", "(RESPOSTA ÚNICA)"), ("Ok?", "(RESPOSTA ÚNICA) Ok?")],
)
def test_unique_reply_prefix_is_always_added(
    initialized_mongo: Any, key: bytes, text: Any, expected_text: str
) -> None:
    attachment = schemas.Attachment(file_name="a.pdf", mimetype="application/pdf", content=b"%PDF")
    message_id = interface(initialized_mongo).send_message(
        "vagas", text=text, attachments=[attachment], expect_unique_reply=True
    )

    document = messages(initialized_mongo).find_one({"_id": ObjectId(message_id)})
    assert main.decrypt_payload(key, document).text == expected_text


def test_unique_reply_prefix_counts_in_the_text_limit(initialized_mongo: Any) -> None:
    with pytest.raises(ValidationError):
        interface(initialized_mongo).send_message(
            "vagas", text="x" * schemas.MAX_TEXT_LENGTH, expect_unique_reply=True
        )
