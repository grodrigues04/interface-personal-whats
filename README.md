# interface-personal-whats

Contrato e cliente da fila de mensagens de WhatsApp do **personalWhats**.

Qualquer automação pessoal envia mensagens só por esta interface: ela valida os dados, confere a
senha de criptografia e grava a mensagem **criptografada** numa fila no MongoDB. Nenhum projeto
fala com o WhatsApp diretamente.

```
projeto ──interface──▶ MongoDB (received)
                         │
         personalWhats/validator: senha e conteúdo ok? ── não ──▶ rejected
                         │ sim
                         ▼
                     validated ──▶ personalWhats/whatsapp ──▶ sent | failed
```

## Instalação

```bash
uv add "interface-personal-whats @ git+https://github.com/grodrigues04/interface-personal-whats.git@main"
# ou
pip install "interface-personal-whats @ git+https://github.com/grodrigues04/interface-personal-whats.git@main"
```

## Uso

```python
from interface_personal_whats import Attachment, InterfacePersonalWhats

whats = InterfacePersonalWhats(mongo_uri=MONGO_URI, encryption_password=ENCRYPTION_PASSWORD)

message_id = whats.send_message(
    source="vagas_automator",            # quem envia: [a-z0-9_-], até 64 caracteres
    text="Relatório da semana",
    recipient="+55 48 99999-8888",       # opcional: sem ele, vai para o contato padrão
    attachments=[Attachment.from_path("relatorio.pdf", caption="3 vagas novas")],
)

whats.get_message(message_id).status    # received, validated, rejected, sending, sent, failed
```

- `recipient`: número com DDI (qualquer formatação), JID de grupo (`...@g.us`) ou de contato
  (`...@s.whatsapp.net`).
- Precisa de `text` ou de pelo menos um anexo. Limites: texto 10.000 caracteres, legenda 1.000,
  até 10 anexos somando 10MB.
- Erros: `pydantic.ValidationError` (dados inválidos), `InvalidPasswordError` (senha errada),
  `PersonalWhatsNotInitializedError` (o validator nunca subiu). Em todos os casos nada é
  enfileirado.

## Especificação (para implementações em outras linguagens)

### Documento na coleção `personal_whats.messages`

| Campo                | Tipo             | Aberto? | Descrição                                        |
|----------------------|------------------|---------|--------------------------------------------------|
| `_id`                | ObjectId         | sim     |                                                  |
| `source`             | string           | sim     | projeto que enviou                               |
| `status`             | string           | sim     | `received` → `validated`/`rejected` → `sending` → `sent`/`failed` |
| `encryption_version` | int              | sim     | `1`                                              |
| `nonce`              | Binary (12 B)    | sim     | nonce do AES-GCM                                 |
| `ciphertext`         | Binary           | —       | payload criptografado + tag GCM de 16 bytes no fim |
| `attempts`           | int              | sim     | tentativas de envio no WhatsApp                  |
| `error`              | string \| null   | sim     | motivo de `rejected`/`failed`                    |
| `created_at`, `updated_at` | Date       | sim     | UTC                                              |
| `validated_at`, `sent_at`  | Date       | sim     | preenchidos pelo personalWhats                   |
| `next_attempt_at`    | Date             | sim     | quando o envio pode ser tentado de novo          |
| `expires_at`         | Date             | sim     | índice TTL: a mensagem some 30 dias após o status final |

### Criptografia (versão 1)

- Chave: `PBKDF2-HMAC-SHA256(senha UTF-8, salt = "personal-whats/v1", 600.000 iterações, 32 bytes)`.
  O salt é fixo para a chave ser derivada uma vez só por processo.
- Cifra: AES-256-GCM, nonce aleatório de 12 bytes por mensagem, sem AAD.
- Payload (texto puro antes de criptografar), JSON UTF-8:

  ```json
  {"text": "...", "recipient": "5548999998888",
   "attachments": [{"file_name": "a.pdf", "mimetype": "application/pdf",
                    "content": "<base64>", "caption": "..."}]}
  ```

- Verificação da senha: o validator grava em `personal_whats.settings` o documento
  `{"_id": "key_check", "nonce": Binary, "ciphertext": Binary}` com o texto
  `personal-whats-key-check` criptografado. Quem consegue descriptografá-lo tem a senha certa.
- `tests/vectors/v1.json` tem vetores de teste (gerados por `scripts/generate_vectors.py`).
  Implementações em JavaScript devem ter testes que descriptografam esses vetores.

## Desenvolvimento

```bash
uv sync
./scripts/test.sh
./scripts/lint.sh
```
