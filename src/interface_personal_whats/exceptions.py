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
