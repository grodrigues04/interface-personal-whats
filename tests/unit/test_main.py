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
    WatchNotFoundError,
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


def incoming(mongo_client: Any) -> Any:
    return mongo_client[main.DEFAULT_DATABASE_NAME][main.INCOMING_COLLECTION]


def watches(mongo_client: Any) -> Any:
    return mongo_client[main.DEFAULT_DATABASE_NAME][main.WATCHES_COLLECTION]


def insert_incoming(mongo_client: Any, key: bytes, watch_id: str, text: str) -> None:
    """Writes a received command like personalWhats/whatsapp does."""
    payload = schemas.IncomingPayload(
        text=text,
        sender_name="Maria",
        sender_jid="5548999998888@s.whatsapp.net",
        quote=schemas.QuotedMessage(
            chat_jid="120363000000000000@g.us",
            message_id=f"ID-{text}",
            participant="5548999998888@s.whatsapp.net",
            text=text,
        ),
    )
    encrypted_data = main.encrypt_model(key, payload)
    incoming(mongo_client).insert_one(
        {
            "watch_id": ObjectId(watch_id),
            "source": "prompt_automator",
            "encryption_version": crypto.ENCRYPTION_VERSION,
            "nonce": encrypted_data.nonce,
            "ciphertext": encrypted_data.ciphertext,
            "received_at": datetime.now(timezone.utc),
            "read_at": None,
        }
    )


def test_watch_chat_stores_encrypted_chat_and_command(initialized_mongo: Any, key: bytes) -> None:
    until = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
    watch_id = interface(initialized_mongo).watch_chat(
        "prompt_automator", chat="120363000000000000@g.us", command="/Prompt", until=until
    )

    document = watches(initialized_mongo).find_one({"_id": ObjectId(watch_id)})
    assert document["source"] == "prompt_automator"
    assert document["stopped_at"] is None
    assert document["expires_at"] == until + main.WATCH_RETENTION
    assert b"120363" not in bytes(document["ciphertext"])
    payload = schemas.WatchPayload.model_validate_json(
        crypto.decrypt(
            key, crypto.EncryptedData(bytes(document["nonce"]), bytes(document["ciphertext"]))
        )
    )
    assert (payload.chat, payload.command) == ("120363000000000000@g.us", "/prompt")


def test_watch_chat_without_until_has_no_expiration(initialized_mongo: Any) -> None:
    watch_id = interface(initialized_mongo).watch_chat("prompt_automator")

    watch = interface(initialized_mongo).get_watch(watch_id)
    assert watch.until is None
    assert watches(initialized_mongo).find_one()["expires_at"] is None


def test_watch_chat_rejects_invalid_command(initialized_mongo: Any) -> None:
    with pytest.raises(ValidationError):
        interface(initialized_mongo).watch_chat("prompt_automator", command="prompt")

    assert watches(initialized_mongo).count_documents({}) == 0


def test_watch_chat_with_wrong_password_stores_nothing(initialized_mongo: Any) -> None:
    with pytest.raises(InvalidPasswordError):
        interface(initialized_mongo, password="errada").watch_chat("prompt_automator")

    assert watches(initialized_mongo).count_documents({}) == 0


def test_stop_watch_sets_stopped_at_and_expiration(initialized_mongo: Any) -> None:
    whats = interface(initialized_mongo)
    watch_id = whats.watch_chat("prompt_automator")

    whats.stop_watch(watch_id)

    watch = whats.get_watch(watch_id)
    assert watch.stopped_at is not None
    assert watches(initialized_mongo).find_one()["expires_at"] is not None


def test_stop_watches_stops_only_the_active_watches_of_the_source(initialized_mongo: Any) -> None:
    whats = interface(initialized_mongo)
    whats.watch_chat("prompt_automator")
    whats.watch_chat("prompt_automator")
    other_watch_id = whats.watch_chat("outro_projeto")

    assert whats.stop_watches("prompt_automator") == 2
    assert whats.get_watch(other_watch_id).stopped_at is None


def test_get_watch_with_unknown_id_raises(initialized_mongo: Any) -> None:
    with pytest.raises(WatchNotFoundError):
        interface(initialized_mongo).get_watch("invalido")


def test_read_new_messages_returns_each_command_once(initialized_mongo: Any, key: bytes) -> None:
    whats = interface(initialized_mongo)
    watch_id = whats.watch_chat("prompt_automator", command="/prompt")
    insert_incoming(initialized_mongo, key, watch_id, "/prompt primeiro")
    insert_incoming(initialized_mongo, key, watch_id, "/prompt segundo")

    first_read = whats.read_new_messages(watch_id)
    second_read = whats.read_new_messages(watch_id)

    assert [message.text for message in first_read] == ["/prompt primeiro", "/prompt segundo"]
    assert first_read[0].sender_name == "Maria"
    assert first_read[0].watch_id == watch_id
    assert first_read[0].quote.message_id == "ID-/prompt primeiro"
    assert second_read == []


def test_read_new_messages_ignores_other_watches(initialized_mongo: Any, key: bytes) -> None:
    whats = interface(initialized_mongo)
    watch_id = whats.watch_chat("prompt_automator")
    other_watch_id = whats.watch_chat("prompt_automator")
    insert_incoming(initialized_mongo, key, other_watch_id, "/liberar")

    assert whats.read_new_messages(watch_id) == []


def test_send_message_with_quoted_message(initialized_mongo: Any, key: bytes) -> None:
    quoted = schemas.QuotedMessage(
        chat_jid="120363000000000000@g.us", message_id="ABC", participant="1@s.whatsapp.net"
    )
    message_id = interface(initialized_mongo).send_message(
        "prompt_automator", text="Na fila", recipient="120363000000000000@g.us", quoted=quoted
    )

    document = messages(initialized_mongo).find_one({"_id": ObjectId(message_id)})
    assert main.decrypt_payload(key, document).quoted == quoted
