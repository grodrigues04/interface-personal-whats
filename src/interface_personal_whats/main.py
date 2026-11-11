import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import Binary, ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from pymongo.collection import Collection

from . import crypto, schemas
from .exceptions import (
    MessageNotFoundError,
    MessageNotSentError,
    PersonalWhatsNotInitializedError,
    ReplyNotExpectedError,
)

log = logging.getLogger(__name__)

DEFAULT_DATABASE_NAME = "personal_whats"
MESSAGES_COLLECTION = "messages"
SETTINGS_COLLECTION = "settings"
KEY_CHECK_ID = "key_check"
# Encrypted fields: never returned by get_message
ENCRYPTED_FIELDS = ("nonce", "ciphertext", "reply_chat", "reply")
DEFAULT_SEND_TIMEOUT = timedelta(minutes=10)

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
    ) -> str:
        """Queues a message and returns its id.

        `source` identifies the project that sends it (ex.: "vagas_automator"). Without
        `recipient`, personalWhats sends it to its default recipient. Invalid content raises
        pydantic.ValidationError and nothing is queued.

        With `expect_unique_reply`, the text starts with UNIQUE_REPLY_PREFIX and personalWhats
        listens to the recipient's chat (and only to it) for REPLY_WINDOW after sending, storing
        only the first reply: see wait_for_reply.
        """
        source = schemas.MessageSource(source=source).source
        if expect_unique_reply:
            text = add_unique_reply_prefix(text)
        payload = schemas.MessagePayload(
            text=text, recipient=recipient, attachments=attachments or []
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

    def close(self) -> None:
        self._client.close()
