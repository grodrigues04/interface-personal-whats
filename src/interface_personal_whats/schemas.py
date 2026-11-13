import mimetypes
import re
from datetime import datetime, timedelta
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
# How long after the message is sent personalWhats listens to its chat for a reply
REPLY_WINDOW = timedelta(minutes=10)
# Added to every message sent with expect_unique_reply: only the first message is the reply
UNIQUE_REPLY_PREFIX = "(RESPOSTA ÚNICA)"
# An emoji can take several code points (skin tone, joiners)
MAX_REACTION_LENGTH = 16

# Phone number with country code (only digits), group JID or contact JID
RECIPIENT_PATTERN = r"^(\d{10,15}|\d+(-\d+)?@g\.us|\d{10,15}@s\.whatsapp\.net)$"
SOURCE_PATTERN = r"^[a-z0-9_-]{1,64}$"
MIMETYPE_PATTERN = r"^[\w.+-]+/[\w.+-]+$"
# Command watched in a chat: "/" followed by letters, digits, "_" or "-" (ex.: "/prompt")
COMMAND_PATTERN = r"^/[\w-]{1,32}$"
# A watched message is a command: its text starts with this character
COMMAND_PREFIX = "/"

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


class QuotedMessage(BaseModel):
    """A received WhatsApp message, quoted ("Responder") by a message sent later.

    Comes from IncomingMessage.quote: projects never build it by hand.
    """

    model_config = ConfigDict(extra="forbid")

    chat_jid: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    # Author of the message in a group chat; None in a contact chat
    participant: Optional[str] = None
    from_me: bool = False
    text: Optional[str] = None


def normalize_chat_address(recipient: Optional[str]) -> Optional[str]:
    """Phone numbers are accepted with any formatting: "+55 (48) 99999-8888"."""
    if not isinstance(recipient, str):
        return recipient
    recipient = recipient.strip()
    if not recipient:
        return None
    if "@" in recipient:
        return recipient
    return re.sub(r"\D", "", recipient)


class MessagePayload(BaseModel):
    """Encrypted part of the message: nobody without the password can read it."""

    model_config = ConfigDict(extra="forbid")

    text: Optional[str] = Field(default=None, max_length=MAX_TEXT_LENGTH)
    # None means the default recipient configured in personalWhats
    recipient: Optional[str] = Field(default=None, pattern=RECIPIENT_PATTERN)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    # The first message sent quotes it. Must be a message of the recipient's chat.
    quoted: Optional[QuotedMessage] = None
    # Emoji reaction to `quoted` instead of a message: no text nor attachments
    reaction: Optional[str] = Field(default=None, min_length=1, max_length=MAX_REACTION_LENGTH)

    @field_validator("text")
    @classmethod
    def empty_text_to_none(cls, text: Optional[str]) -> Optional[str]:
        return text if text and text.strip() else None

    @field_validator("recipient", mode="before")
    @classmethod
    def normalize_recipient(cls, recipient: Optional[str]) -> Optional[str]:
        return normalize_chat_address(recipient)

    @model_validator(mode="after")
    def check_content(self) -> "MessagePayload":
        if self.reaction is not None:
            if self.quoted is None:
                raise ValueError("a reação precisa da mensagem citada (quoted)")  # noqa: TRY003
            if self.text is not None or self.attachments:
                raise ValueError("a reação não tem texto nem anexo")  # noqa: TRY003
            return self
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
    expect_unique_reply: bool = False
    reply_deadline: Optional[datetime] = None
    replied_at: Optional[datetime] = None


class ReplyPayload(BaseModel):
    """Encrypted part of a reply, written by personalWhats/whatsapp."""

    text: Optional[str] = None
    # WhatsApp content type: "conversation", "extendedTextMessage", "imageMessage",
    # "audioMessage"... Media is not downloaded: only its caption comes in `text`.
    message_type: str


class Reply(ReplyPayload):
    received_at: datetime


class WatchPayload(BaseModel):
    """Encrypted part of a chat watch: which chat personalWhats listens to."""

    model_config = ConfigDict(extra="forbid")

    # Same formats as MessagePayload.recipient. None means the default recipient.
    chat: Optional[str] = Field(default=None, pattern=RECIPIENT_PATTERN)
    # None: every command (message starting with "/"). Otherwise only this command.
    command: Optional[str] = Field(default=None, pattern=COMMAND_PATTERN)

    @field_validator("chat", mode="before")
    @classmethod
    def normalize_chat(cls, chat: Optional[str]) -> Optional[str]:
        return normalize_chat_address(chat)

    @field_validator("command", mode="before")
    @classmethod
    def lowercase_command(cls, command: Optional[str]) -> Optional[str]:
        return command.strip().lower() if isinstance(command, str) else command


class WatchInfo(BaseModel):
    """Public part of a chat watch."""

    id: str
    source: str
    created_at: datetime
    # None: listens until stopped
    until: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    # Edits of the commands are stored too (edited_message_id of IncomingMessage)
    include_edits: bool = False


class IncomingPayload(BaseModel):
    """Encrypted part of a command received in a watched chat, written by personalWhats/whatsapp."""

    text: str
    # WhatsApp name of the author ("pushName"); None when unknown
    sender_name: Optional[str] = None
    # JID of the author in a group, or of the contact
    sender_jid: Optional[str] = None
    # Sent from the phone of the personalWhats account (Gustavo himself)
    from_me: bool = False
    # Pass to send_message(quoted=...) to reply quoting this message
    quote: QuotedMessage
    # Only in watches with include_edits: this message is an edit of the command whose
    # quote.message_id is this id, and `text` is the new text. `quote` points to the original.
    edited_message_id: Optional[str] = None


class IncomingMessage(IncomingPayload):
    id: str
    watch_id: str
    received_at: datetime
