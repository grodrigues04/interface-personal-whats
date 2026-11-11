import logging

from .exceptions import (
    InvalidPasswordError,
    MessageNotFoundError,
    PersonalWhatsError,
    PersonalWhatsNotInitializedError,
)
from .main import InterfacePersonalWhats
from .schemas import Attachment, MessageInfo, MessagePayload, MessageStatus
from .version import VERSION

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = VERSION

__all__ = [
    "Attachment",
    "InterfacePersonalWhats",
    "InvalidPasswordError",
    "MessageInfo",
    "MessageNotFoundError",
    "MessagePayload",
    "MessageStatus",
    "PersonalWhatsError",
    "PersonalWhatsNotInitializedError",
]
