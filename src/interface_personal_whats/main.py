import logging
from datetime import datetime, timezone
from typing import Any, Optional

from bson import Binary, ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from pymongo.collection import Collection

from . import crypto, schemas
from .exceptions import MessageNotFoundError, PersonalWhatsNotInitializedError

log = logging.getLogger(__name__)

DEFAULT_DATABASE_NAME = "personal_whats"
MESSAGES_COLLECTION = "messages"
SETTINGS_COLLECTION = "settings"
KEY_CHECK_ID = "key_check"

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


def build_message_document(
    source: str, encrypted_data: crypto.EncryptedData, now: Optional[datetime] = None
) -> Document:
    now = now or datetime.now(timezone.utc)
    return {
        "source": source,
        "status": schemas.MessageStatus.RECEIVED.value,
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
    ) -> str:
        """Queues a message and returns its id.

        `source` identifies the project that sends it (ex.: "vagas_automator"). Without
        `recipient`, personalWhats sends it to its default recipient. Invalid content raises
        pydantic.ValidationError and nothing is queued.
        """
        source = schemas.MessageSource(source=source).source
        payload = schemas.MessagePayload(
            text=text, recipient=recipient, attachments=attachments or []
        )
        if not self._is_password_checked:
            self.check_password()

        document = build_message_document(source, encrypt_payload(self._key, payload))
        message_id = str(self._messages.insert_one(document).inserted_id)
        log.info(f"Message {message_id} queued by {source}")
        return message_id

    def get_message(self, message_id: str) -> schemas.MessageInfo:
        try:
            object_id = ObjectId(message_id)
        except (InvalidId, TypeError) as error:
            raise MessageNotFoundError(message_id) from error
        document = self._messages.find_one(
            {"_id": object_id}, projection={"nonce": False, "ciphertext": False}
        )
        if document is None:
            raise MessageNotFoundError(message_id)
        return schemas.MessageInfo.model_validate({**document, "id": str(document["_id"])})

    def close(self) -> None:
        self._client.close()
