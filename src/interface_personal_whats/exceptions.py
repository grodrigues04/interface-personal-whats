class PersonalWhatsError(Exception):
    """Base error of the interface."""


class InvalidPasswordError(PersonalWhatsError):
    def __init__(self) -> None:
        super().__init__("Senha de criptografia inválida")


class PersonalWhatsNotInitializedError(PersonalWhatsError):
    def __init__(self) -> None:
        super().__init__(
            "O personalWhats ainda não foi inicializado: suba o validator para criar a "
            "verificação da senha"
        )


class MessageNotFoundError(PersonalWhatsError):
    def __init__(self, message_id: str) -> None:
        super().__init__(f"Mensagem {message_id} não encontrada")


class ReplyNotExpectedError(PersonalWhatsError):
    def __init__(self, message_id: str) -> None:
        super().__init__(f"Mensagem {message_id} foi enviada sem expect_unique_reply=True")


class MessageNotSentError(PersonalWhatsError):
    def __init__(self, message_id: str, reason: str) -> None:
        super().__init__(f"Mensagem {message_id} não foi enviada: {reason}")


class WatchNotFoundError(PersonalWhatsError):
    def __init__(self, watch_id: str) -> None:
        super().__init__(f"Escuta {watch_id} não encontrada")
