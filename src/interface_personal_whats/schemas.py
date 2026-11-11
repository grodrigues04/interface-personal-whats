import mimetypes
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_TEXT_LENGTH = 10_000
MAX_CAPTION_LENGTH = 1_000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENTS_SIZE = 10 * 1024 * 1024

# Phone number with country code (only digits), group JID or contact JID
RECIPIENT_PATTERN = r"^(\d{10,15}|\d+(-\d+)?@g\.us|\d{10,15}@s\.whatsapp\.net)$"
SOURCE_PATTERN = r"^[a-z0-9_-]{1,64}$"
MIMETYPE_PATTERN = r"^[\w.+-]+/[\w.+-]+$"

Source = Annotated[str, StringConstraints(pattern=SOURCE_PATTERN)]


class MessageStatus(str, Enum):
    RECEIVED = "received"  # inserted by the interface, not checked yet
    VALIDATED = "validated"  # password and content checked by the validator
    REJECTED = "rejected"  # wrong password or invalid content: never reaches WhatsApp
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"  # validated, but WhatsApp failed every attempt


FINAL_STATUSES = (MessageStatus.REJECTED, MessageStatus.SENT, MessageStatus.FAILED)


class MessageSource(BaseModel):
    """Project that sends the message. Stays readable, so it can be listed without the password."""

    source: Source


class Attachment(BaseModel):
    # `content` travels as base64 inside the encrypted JSON
    model_config = ConfigDict(extra="forbid", ser_json_bytes="base64", val_json_bytes="base64")

    file_name: str = Field(min_length=1, max_length=255)
    mimetype: str = Field(pattern=MIMETYPE_PATTERN)
    content: bytes = Field(min_length=1)
    caption: Optional[str] = Field(default=None, max_length=MAX_CAPTION_LENGTH)

    @classmethod
    def from_path(
        cls,
        file_path: Union[str, Path],
        caption: Optional[str] = None,
        mimetype: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> "Attachment":
        file_path = Path(file_path)
        guessed_mimetype, _ = mimetypes.guess_type(file_path.name)
        return cls(
            file_name=file_name or file_path.name,
            mimetype=mimetype or guessed_mimetype or "application/octet-stream",
            content=file_path.read_bytes(),
            caption=caption,
        )


class MessagePayload(BaseModel):
    """Encrypted part of the message: nobody without the password can read it."""

    model_config = ConfigDict(extra="forbid")

    text: Optional[str] = Field(default=None, max_length=MAX_TEXT_LENGTH)
    # None means the default recipient configured in personalWhats
    recipient: Optional[str] = Field(default=None, pattern=RECIPIENT_PATTERN)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

    @field_validator("text")
    @classmethod
    def empty_text_to_none(cls, text: Optional[str]) -> Optional[str]:
        return text if text and text.strip() else None

    @field_validator("recipient", mode="before")
    @classmethod
    def normalize_recipient(cls, recipient: Optional[str]) -> Optional[str]:
        """Phone numbers are accepted with any formatting: "+55 (48) 99999-8888"."""
        if not isinstance(recipient, str):
            return recipient
        recipient = recipient.strip()
        if not recipient:
            return None
        if "@" in recipient:
            return recipient
        return re.sub(r"\D", "", recipient)

    @model_validator(mode="after")
    def check_content(self) -> "MessagePayload":
        if self.text is None and not self.attachments:
            raise ValueError("a mensagem precisa de texto ou de anexo")  # noqa: TRY003
        attachments_size = sum(len(attachment.content) for attachment in self.attachments)
        if attachments_size > MAX_ATTACHMENTS_SIZE:
            raise ValueError(  # noqa: TRY003
                f"os anexos somam {attachments_size} bytes, o máximo é {MAX_ATTACHMENTS_SIZE}"
            )
        return self


class MessageInfo(BaseModel):
    """Public part of a queued message (no decryption needed)."""

    id: str
    source: str
    status: MessageStatus
    attempts: int
    error: Optional[str]
    created_at: datetime
    updated_at: datetime
    sent_at: Optional[datetime] = None
