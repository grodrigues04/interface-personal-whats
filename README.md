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

### Esperar a resposta

```python
message_id = whats.send_message(source="meu_projeto", text="Posso aplicar?", expect_unique_reply=True)
reply = whats.wait_for_reply(message_id)   # None se ninguém respondeu em 10 minutos
if reply is not None:
    print(reply.text, reply.message_type, reply.received_at)
```

- O texto sempre começa com `(RESPOSTA ÚNICA)` (`UNIQUE_REPLY_PREFIX`): só a primeira mensagem
  conta como resposta. O prefixo entra no limite de 10.000 caracteres.
- O personalWhats escuta só o chat para onde a mensagem foi, por `REPLY_WINDOW` (10 minutos)
  após o envio, e guarda a primeira mensagem escrita ali (criptografada). Nenhum outro chat é lido.
- Várias mensagens esperando resposta no mesmo chat ao mesmo tempo: se a resposta citar uma delas
  ("Responder" no WhatsApp), vai para a citada; sem citação, vai para a última enviada. Cada
  resposta conta para uma mensagem só, e as outras seguem esperando até o prazo delas:

  ```
  13:37  envia (A) "(RESPOSTA ÚNICA) ..."   → espera até 13:47
  13:38  envia (B) "(RESPOSTA ÚNICA) ..."   → espera até 13:48
  13:39  chega "sim", sem citar nada        → resposta de (B); (A) segue esperando
  13:40  chega "não", citando (A)           → resposta de (A)
  ```
- A mensagem aceita como resposta ganha uma reação ✅ no WhatsApp assim que é guardada.
- Mídia não é baixada: `text` traz a legenda e `message_type` o tipo (`audioMessage`...).
- `wait_for_reply` levanta `MessageNotSentError` (rejeitada, falhou, ou não enviada em
  `send_timeout`, padrão 10 minutos) e `ReplyNotExpectedError` (enviada sem `expect_unique_reply`).
  `get_reply(message_id)` consulta sem bloquear.
- Chat usado por mais de um projeto (ex.: grupo Squeaker): antes de perguntar, espere enquanto
  `whats.is_awaiting_reply(recipient)` for `True`, para nunca haver duas perguntas abertas no
  mesmo chat dividindo as respostas.

### Ler comandos de um chat

```python
watch_id = whats.watch_chat(
    source="meu_projeto",
    chat="120363000000000000@g.us",   # opcional: sem ele, o contato padrão (grupo Claudio)
    command="/prompt",                # opcional: sem ele, todo comando (mensagem que começa com "/")
    until=datetime.now(timezone.utc) + timedelta(minutes=30),  # opcional: sem ele, até stop_watch
)
for message in whats.read_new_messages(watch_id):   # cada comando volta uma vez só
    message.text, message.sender_name, message.from_me, message.received_at
    whats.send_message(source="meu_projeto", text="Recebido", recipient="120363000000000000@g.us",
                       quoted=message.quote)        # responde citando ("Responder") o comando
whats.stop_watch(watch_id)            # ou whats.stop_watches("meu_projeto") ao iniciar
```

- Só mensagens que começam com `/` são guardadas, e com `command` só as que têm esse comando
  como primeira palavra (sem diferenciar maiúsculas). Todo o resto do chat é descartado sem ser
  lido; nenhum outro chat é olhado.
- Contam mensagens escritas depois do início da escuta (e antes de `until`), inclusive as do
  celular da própria conta (`from_me`). Mensagens enviadas pelo personalWhats nunca contam.
- Um comando guardado por uma escuta **não** conta como resposta de `expect_unique_reply`.
- O personalWhats lê as escutas a cada 5 segundos: comandos enviados nos primeiros segundos de
  uma escuta nova podem se perder.
- `quoted` precisa ser uma mensagem do chat do `recipient`. Só a primeira mensagem enviada cita.

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
| `expect_unique_reply`       | bool             | sim     | o personalWhats deve esperar a resposta          |
| `reply_deadline`     | Date             | sim     | até quando a resposta é aceita (`sent_at` + 10 min) |
| `reply_chat`         | `{nonce, ciphertext}` | —  | JSON `{chat_jids, sent_message_ids, sent_at}`, para o personalWhats retomar a espera após reiniciar |
| `reply`              | `{nonce, ciphertext}` | —  | JSON `{"text": "...", "message_type": "conversation"}` |
| `replied_at`         | Date             | sim     | quando a resposta chegou                         |

### Coleções `personal_whats.watches` e `personal_whats.incoming`

`watches` (gravado pela interface): `source`, `encryption_version`, `nonce`, `ciphertext` (JSON
`{"chat": "..." | null, "command": "/prompt" | null}`), `created_at`, `until` (Date ou null),
`stopped_at` (Date ou null) e `expires_at` (TTL: 30 dias após o fim).

`incoming` (gravado pelo personalWhats): `watch_id`, `whatsapp_message_id` (único por escuta),
`encryption_version`, `nonce`, `ciphertext`, `received_at`, `read_at` (preenchido por
`read_new_messages`) e `expires_at` (TTL de 30 dias). O conteúdo criptografado é o JSON
`{"text", "sender_name", "sender_jid", "from_me", "quote": {"chat_jid", "message_id",
"participant", "from_me", "text"}}`.

### Criptografia (versão 1)

- Chave: `PBKDF2-HMAC-SHA256(senha UTF-8, salt = "personal-whats/v1", 600.000 iterações, 32 bytes)`.
  O salt é fixo para a chave ser derivada uma vez só por processo.
- Cifra: AES-256-GCM, nonce aleatório de 12 bytes por mensagem, sem AAD.
- Payload (texto puro antes de criptografar), JSON UTF-8:

  ```json
  {"text": "...", "recipient": "5548999998888",
   "attachments": [{"file_name": "a.pdf", "mimetype": "application/pdf",
                    "content": "<base64>", "caption": "..."}],
   "quoted": null}
  ```

  `quoted` (opcional) é `{"chat_jid", "message_id", "participant", "from_me", "text"}`.

- `reply_chat` e `reply` usam a mesma chave e cifra, cada um com seu nonce.
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
