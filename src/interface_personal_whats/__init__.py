import logging

from .exceptions import (
    InvalidPasswordError,
    MessageNotFoundError,
    MessageNotSentError,
    PersonalWhatsError,
    PersonalWhatsNotInitializedError,
    ReplyNotExpectedError,
    WatchNotFoundError,
)
from .main import InterfacePersonalWhats
from .schemas import (
    REPLY_WINDOW,
    UNIQUE_REPLY_PREFIX,
    Attachment,
    IncomingMessage,
    MessageInfo,
    MessagePayload,
    MessageStatus,
    QuotedMessage,
    Reply,
    WatchInfo,
)
from .version import VERSION

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = VERSION

__all__ = [
    "REPLY_WINDOW",
    "UNIQUE_REPLY_PREFIX",
    "Attachment",
    "IncomingMessage",
    "InterfacePersonalWhats",
    "InvalidPasswordError",
    "MessageInfo",
    "MessageNotFoundError",
    "MessageNotSentError",
    "MessagePayload",
    "MessageStatus",
    "PersonalWhatsError",
    "PersonalWhatsNotInitializedError",
    "QuotedMessage",
    "Reply",
    "ReplyNotExpectedError",
    "WatchInfo",
    "WatchNotFoundError",
]
