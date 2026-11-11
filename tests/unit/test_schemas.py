from pathlib import Path

import pytest
from pydantic import ValidationError

from interface_personal_whats import schemas


def attachment(content: bytes = b"pdf") -> schemas.Attachment:
    return schemas.Attachment(file_name="a.pdf", mimetype="application/pdf", content=content)


def test_message_needs_text_or_attachment() -> None:
    with pytest.raises(ValidationError, match="texto ou de anexo"):
        schemas.MessagePayload(text="   ")


def test_message_with_only_attachment_is_valid() -> None:
    payload = schemas.MessagePayload(attachments=[attachment()])

    assert payload.text is None


@pytest.mark.parametrize(
    ("recipient", "expected"),
    [
        ("+55 (48) 99999-8888", "5548999998888"),
        ("120363427990271477@g.us", "120363427990271477@g.us"),
        ("554799917865-1632958749@g.us", "554799917865-1632958749@g.us"),
        ("5548999998888@s.whatsapp.net", "5548999998888@s.whatsapp.net"),
        ("", None),
        (None, None),
    ],
)
def test_recipient_is_normalized(recipient: str, expected: str) -> None:
    assert schemas.MessagePayload(text="oi", recipient=recipient).recipient == expected


@pytest.mark.parametrize("recipient", ["123", "abc@g.us", "5548999998888@outro.com"])
def test_invalid_recipient_is_rejected(recipient: str) -> None:
    with pytest.raises(ValidationError):
        schemas.MessagePayload(text="oi", recipient=recipient)


def test_attachments_over_size_limit_are_rejected() -> None:
    with pytest.raises(ValidationError, match="os anexos somam"):
        schemas.MessagePayload(
            attachments=[attachment(b"x" * (schemas.MAX_ATTACHMENTS_SIZE // 2 + 1))] * 2
        )


def test_payload_json_round_trip_keeps_attachment_bytes() -> None:
    payload = schemas.MessagePayload(text="oi", attachments=[attachment(b"\x00\xffbinario")])

    parsed = schemas.MessagePayload.model_validate_json(payload.model_dump_json())

    assert parsed == payload
    assert '"content":"AP9iaW5hcmlv"' in payload.model_dump_json()


def test_unknown_payload_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schemas.MessagePayload.model_validate_json('{"text": "oi", "priority": 1}')


def test_attachment_from_path_guesses_mimetype(tmp_path: Path) -> None:
    file_path = tmp_path / "relatorio.pdf"
    file_path.write_bytes(b"%PDF")

    result = schemas.Attachment.from_path(file_path, caption="Resumo")

    assert (result.file_name, result.mimetype, result.content, result.caption) == (
        "relatorio.pdf",
        "application/pdf",
        b"%PDF",
        "Resumo",
    )


@pytest.mark.parametrize("source", ["Vagas", "vagas automator", "", "a" * 65])
def test_invalid_source_is_rejected(source: str) -> None:
    with pytest.raises(ValidationError):
        schemas.MessageSource(source=source)
