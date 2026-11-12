import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import Binary, ObjectId
from bson.errors import InvalidId
from pydantic import ValidationError
from pymongo import MongoClient
from pymongo.collection import Collection

from . import crypto, schemas
from .exceptions import (
    InvalidPasswordError,
    MessageNotFoundError,
    MessageNotSentError,
    PersonalWhatsNotInitializedError,
    ReplyNotExpectedError,
    WatchNotFoundError,
)

log = logging.getLogger(__name__)

DEFAULT_DATABASE_NAME = "personal_whats"
MESSAGES_COLLECTION = "messages"
SETTINGS_COLLECTION = "settings"
WATCHES_COLLECTION = "watches"
INCOMING_COLLECTION = "incoming"
KEY_CHECK_ID = "key_check"
# Encrypted fields: never returned by get_message
ENCRYPTED_FIELDS = ("nonce", "ciphertext", "reply_chat", "reply")
DEFAULT_SEND_TIMEOUT = timedelta(minutes=10)
# Watches and the commands they received are removed this long after the watch ends
WATCH_RETENTION = timedelta(days=30)

Document = dict[str, Any]


def encrypt_payload(key: bytes, payload: schemas.MessagePayload) -> crypto.EncryptedData:
    return crypto.encrypt(key, payload.model_dump_json().encode("utf-8"))


def decrypt_payload(key: bytes, message_document: Document) -> schemas.MessagePayload:
    """Raises InvalidPasswordError (wrong key) or pydantic.ValidationError (invalid content)."""
    plaintext = crypto.decrypt(
        key,
        crypto.EncryptedData(
            nonce=bytes(message_document["nonce"]),
            ciphertext=bytes(message_document["ciphertext"]),
        ),
    )
    return schemas.MessagePayload.model_validate_json(plaintext)


def encrypt_model(key: bytes, model: Any) -> crypto.EncryptedData:
    return crypto.encrypt(key, model.model_dump_json().encode("utf-8"))


def decrypt_incoming(key: bytes, incoming_document: Document) -> schemas.IncomingMessage:
    """Raises InvalidPasswordError (wrong key)."""
    plaintext = crypto.decrypt(
        key,
        crypto.EncryptedData(
            nonce=bytes(incoming_document["nonce"]),
            ciphertext=bytes(incoming_document["ciphertext"]),
        ),
    )
    incoming_payload = schemas.IncomingPayload.model_validate_json(plaintext)
    return schemas.IncomingMessage(
        **incoming_payload.model_dump(),
        id=str(incoming_document["_id"]),
        watch_id=str(incoming_document["watch_id"]),
        received_at=incoming_document["received_at"],
    )


def build_watch_document(
    source: str,
    encrypted_data: crypto.EncryptedData,
    until: Optional[datetime],
    now: Optional[datetime] = None,
) -> Document:
    now = now or datetime.now(timezone.utc)
    return {
        "source": source,
        "encryption_version": crypto.ENCRYPTION_VERSION,
        "nonce": Binary(encrypted_data.nonce),
        "ciphertext": Binary(encrypted_data.ciphertext),
        "created_at": now,
        "until": until,
        "stopped_at": None,
        # Removed by the TTL index after it ends; a watch without `until` is kept while active
        "expires_at": until + WATCH_RETENTION if until is not None else None,
    }


def add_unique_reply_prefix(text: Optional[str]) -> str:
    if text is None or not text.strip():
        return schemas.UNIQUE_REPLY_PREFIX
    return f"{schemas.UNIQUE_REPLY_PREFIX} {text}"


def decrypt_reply(key: bytes, message_document: Document) -> Optional[schemas.Reply]:
    """Returns None while nobody replied. Raises InvalidPasswordError (wrong key)."""
    encrypted_reply = message_document.get("reply")
    if encrypted_reply is None:
        return None
    plaintext = crypto.decrypt(
        key,
        crypto.EncryptedData(
            nonce=bytes(encrypted_reply["nonce"]),
            ciphertext=bytes(encrypted_reply["ciphertext"]),
        ),
    )
    reply_payload = schemas.ReplyPayload.model_validate_json(plaintext)
    return schemas.Reply(**reply_payload.model_dump(), received_at=message_document["replied_at"])


def find_reply_deadline(message_document: Document) -> Optional[datetime]:
    """None while the message was not sent.

    personalWhats versions without replies leave `reply_deadline` empty: the window is then
    counted from `sent_at`.
    """
    reply_deadline: Optional[datetime] = message_document.get("reply_deadline")
    sent_at: Optional[datetime] = message_document.get("sent_at")
    if reply_deadline is None and sent_at is not None:
        reply_deadline = sent_at + schemas.REPLY_WINDOW
    return reply_deadline


def build_message_document(
    source: str,
    encrypted_data: crypto.EncryptedData,
    now: Optional[datetime] = None,
    expect_unique_reply: bool = False,
) -> Document:
    now = now or datetime.now(timezone.utc)
    return {
        "source": source,
        "status": schemas.MessageStatus.RECEIVED.value,
        "expect_unique_reply": expect_unique_reply,
        "encryption_version": crypto.ENCRYPTION_VERSION,
        "nonce": Binary(encrypted_data.nonce),
        "ciphertext": Binary(encrypted_data.ciphertext),
        "attempts": 0,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def read_key_check(settings: Collection[Document]) -> Optional[crypto.EncryptedData]:
    key_check = settings.find_one({"_id": KEY_CHECK_ID})
    if key_check is None:
        return None
    return crypto.EncryptedData(
        nonce=bytes(key_check["nonce"]), ciphertext=bytes(key_check["ciphertext"])
    )


class InterfacePersonalWhats:
    def __init__(
        self,
        mongo_uri: str,
        encryption_password: str,
        database_name: str = DEFAULT_DATABASE_NAME,
        server_selection_timeout_ms: int = 15_000,
        mongo_client: Optional[MongoClient[Document]] = None,
    ):
        self._client: MongoClient[Document] = mongo_client or MongoClient(
            mongo_uri, serverSelectionTimeoutMS=server_selection_timeout_ms, tz_aware=True
        )
        database = self._client[database_name]
        self._messages: Collection[Document] = database[MESSAGES_COLLECTION]
        self._settings: Collection[Document] = database[SETTINGS_COLLECTION]
        self._watches: Collection[Document] = database[WATCHES_COLLECTION]
        self._incoming: Collection[Document] = database[INCOMING_COLLECTION]
        self._key = crypto.derive_key(encryption_password)
        self._is_password_checked = False

    def check_password(self) -> None:
        """Compares the password with the key check written by the validator.

        Raises InvalidPasswordError or PersonalWhatsNotInitializedError.
        """
        key_check = read_key_check(self._settings)
        if key_check is None:
            raise PersonalWhatsNotInitializedError()
        crypto.verify_key_check(self._key, key_check)
        self._is_password_checked = True

    def send_message(
        self,
        source: str,
        text: Optional[str] = None,
        recipient: Optional[str] = None,
        attachments: Optional[list[schemas.Attachment]] = None,
        expect_unique_reply: bool = False,
        quoted: Optional[schemas.QuotedMessage] = None,
    ) -> str:
        """Queues a message and returns its id.

        `source` identifies the project that sends it (ex.: "vagas_automator"). Without
        `recipient`, personalWhats sends it to its default recipient. Invalid content raises
        pydantic.ValidationError and nothing is queued.

        `quoted` (IncomingMessage.quote) makes the first message sent reply to ("Responder")
        that received message, which must be in the recipient's chat.

        With `expect_unique_reply`, the text starts with UNIQUE_REPLY_PREFIX and personalWhats
        listens to the recipient's chat (and only to it) for REPLY_WINDOW after sending, storing
        only the first reply: see wait_for_reply.
        """
        source = schemas.MessageSource(source=source).source
        if expect_unique_reply:
            text = add_unique_reply_prefix(text)
        payload = schemas.MessagePayload(
            text=text, recipient=recipient, attachments=attachments or [], quoted=quoted
        )
        if not self._is_password_checked:
            self.check_password()

        document = build_message_document(
            source, encrypt_payload(self._key, payload), expect_unique_reply=expect_unique_reply
        )
        message_id = str(self._messages.insert_one(document).inserted_id)
        log.info(f"Message {message_id} queued by {source}")
        return message_id

    def _find_message(self, message_id: str, projection: Optional[Document] = None) -> Document:
        try:
            object_id = ObjectId(message_id)
        except (InvalidId, TypeError) as error:
            raise MessageNotFoundError(message_id) from error
        document = self._messages.find_one({"_id": object_id}, projection=projection)
        if document is None:
            raise MessageNotFoundError(message_id)
        return document

    def get_message(self, message_id: str) -> schemas.MessageInfo:
        document = self._find_message(message_id, projection=dict.fromkeys(ENCRYPTED_FIELDS, False))
        return schemas.MessageInfo.model_validate({**document, "id": str(document["_id"])})

    def get_reply(self, message_id: str) -> Optional[schemas.Reply]:
        """First reply to a message sent with expect_unique_reply, or None while there is none."""
        document = self._find_message(message_id)
        if not document.get("expect_unique_reply"):
            raise ReplyNotExpectedError(message_id)
        return decrypt_reply(self._key, document)

    def is_awaiting_reply(self, recipient: Optional[str] = None) -> bool:
        """True while a message with expect_unique_reply to this chat still waits for its reply.

        Counts the messages of every project. Without `recipient`, the default recipient.

        Two messages waiting in the same chat share the replies (see wait_for_reply): projects
        that ask in a shared chat call this first and wait while it is True.
        """
        expected_recipient = schemas.normalize_chat_address(recipient)
        now = datetime.now(timezone.utc)
        waiting_documents = self._messages.find(
            {
                "expect_unique_reply": True,
                "reply": {"$exists": False},
                "status": {
                    "$in": [
                        schemas.MessageStatus.RECEIVED.value,
                        schemas.MessageStatus.VALIDATED.value,
                        schemas.MessageStatus.SENDING.value,
                        schemas.MessageStatus.SENT.value,
                    ]
                },
            }
        )
        for document in waiting_documents:
            reply_deadline = find_reply_deadline(document)
            if reply_deadline is not None and now >= reply_deadline:
                continue
            try:
                payload = decrypt_payload(self._key, document)
            except (InvalidPasswordError, ValidationError):
                continue
            if payload.recipient == expected_recipient:
                return True
        return False

    def wait_for_reply(
        self,
        message_id: str,
        send_timeout: timedelta = DEFAULT_SEND_TIMEOUT,
        poll_interval_seconds: float = 5,
    ) -> Optional[schemas.Reply]:
        """Blocks until the message sent with expect_unique_reply gets a reply.

        Returns None when nobody replied within REPLY_WINDOW (10 minutes) after the message was
        sent. Raises MessageNotSentError when the message was rejected, failed, or was not sent
        within `send_timeout` after being queued (personalWhats stopped, for example).
        """
        while True:
            document = self._find_message(message_id)
            if not document.get("expect_unique_reply"):
                raise ReplyNotExpectedError(message_id)
            reply = decrypt_reply(self._key, document)
            if reply is not None:
                return reply

            now = datetime.now(timezone.utc)
            status = schemas.MessageStatus(document["status"])
            if status in (schemas.MessageStatus.REJECTED, schemas.MessageStatus.FAILED):
                raise MessageNotSentError(message_id, f"{status.value}: {document.get('error')}")
            reply_deadline = find_reply_deadline(document)
            if reply_deadline is None:
                limit = document["created_at"] + send_timeout
                if now >= limit:
                    raise MessageNotSentError(
                        message_id, f"ainda está {status.value} após {send_timeout}"
                    )
            elif now >= reply_deadline:
                log.info(f"Message {message_id} got no reply until {reply_deadline}")
                return None
            else:
                limit = reply_deadline
            time.sleep(min(poll_interval_seconds, (limit - now).total_seconds()))

    def watch_chat(
        self,
        source: str,
        chat: Optional[str] = None,
        command: Optional[str] = None,
        until: Optional[datetime] = None,
    ) -> str:
        """Starts listening to the commands written in a chat and returns the watch id.

        Only messages starting with "/" are stored, and with `command` (ex.: "/prompt") only
        that command; every other message of the chat is discarded unread. Without `chat`, the
        default recipient (group Claudio) is watched. Without `until`, the watch lasts until
        stop_watch. Read what arrived with read_new_messages.
        """
        source = schemas.MessageSource(source=source).source
        payload = schemas.WatchPayload(chat=chat, command=command)
        if until is not None and until.tzinfo is None:
            raise ValueError("until must be timezone-aware")  # noqa: TRY003
        if not self._is_password_checked:
            self.check_password()

        document = build_watch_document(source, encrypt_model(self._key, payload), until)
        watch_id = str(self._watches.insert_one(document).inserted_id)
        log.info(f"Watch {watch_id} started by {source}")
        return watch_id

    def _find_watch(self, watch_id: str) -> Document:
        try:
            object_id = ObjectId(watch_id)
        except (InvalidId, TypeError) as error:
            raise WatchNotFoundError(watch_id) from error
        document = self._watches.find_one({"_id": object_id})
        if document is None:
            raise WatchNotFoundError(watch_id)
        return document

    def get_watch(self, watch_id: str) -> schemas.WatchInfo:
        document = self._find_watch(watch_id)
        return schemas.WatchInfo.model_validate({**document, "id": str(document["_id"])})

    def stop_watch(self, watch_id: str) -> None:
        """Stops the watch: personalWhats stops storing its commands within a few seconds."""
        document = self._find_watch(watch_id)
        if document.get("stopped_at") is not None:
            return
        now = datetime.now(timezone.utc)
        self._watches.update_one(
            {"_id": document["_id"]},
            {"$set": {"stopped_at": now, "expires_at": now + WATCH_RETENTION}},
        )
        log.info(f"Watch {watch_id} stopped")

    def stop_watches(self, source: str) -> int:
        """Stops every active watch of a project (ex.: left by a previous run). Returns how many."""
        now = datetime.now(timezone.utc)
        result = self._watches.update_many(
            {"source": source, "stopped_at": None},
            {"$set": {"stopped_at": now, "expires_at": now + WATCH_RETENTION}},
        )
        return result.modified_count

    def read_new_messages(self, watch_id: str) -> list[schemas.IncomingMessage]:
        """Commands received by the watch and not read yet, oldest first.

        Each command is returned only once: it is marked as read here.
        """
        watch = self._find_watch(watch_id)
        documents = list(
            self._incoming.find({"watch_id": watch["_id"], "read_at": None}).sort(
                [("received_at", 1), ("_id", 1)]
            )
        )
        if not documents:
            return []
        self._incoming.update_many(
            {"_id": {"$in": [document["_id"] for document in documents]}},
            {"$set": {"read_at": datetime.now(timezone.utc)}},
        )
        return [decrypt_incoming(self._key, document) for document in documents]

    def close(self) -> None:
        self._client.close()
