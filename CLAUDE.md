# interface-personal-whats

Contrato (schemas, criptografia, documento no MongoDB) e cliente da fila de mensagens de
WhatsApp. É instalado via `git+...@main` por todos os projetos de `~/MyProjects` que enviam
mensagens: um push na `main` muda o contrato de todos eles. Leia também `~/MyProjects/CLAUDE.md`
(carregado junto) e o `README.md`.

## Toda alteração aqui precisa ser propagada

Ao terminar qualquer alteração que mude o uso ou o contrato, **na mesma tarefa**:

1. **Atualizar `~/MyProjects/CLAUDE.md`**, seção "Enviar mensagem de WhatsApp a partir de um
   projeto" (exemplo de código, regras do contrato, limites, erros), e o `README.md` daqui.
2. **Atualizar os projetos que usam a interface.** Para encontrá-los (inclui projetos criados
   depois deste arquivo):

   ```bash
   grep -rlE 'interface_personal_whats|interface-personal-whats|personal-whats/v1' ~/MyProjects \
     --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.next --exclude-dir=.git \
     --exclude='*.md' --exclude='*.lock'
   ```

   Sempre afetados por mudanças de contrato:
   - `personalWhats/validator` (Python, usa a interface) e `personalWhats/whatsapp` (Node:
     `crypto.js` reimplementa a descriptografia e `outgoing.js` lê o payload);
   - `personal-whats-front` (TypeScript: `src/lib/encryption/decryption.ts` e
     `src/lib/messages/statuses.ts`);
   - todo projeto que chama `send_message` (hoje `vagas_automator/runner/send_results.py`).
3. **Criptografia ou formato do payload mudou:** rodar `uv run python scripts/generate_vectors.py`
   e copiar `tests/vectors/v1.json` para `personalWhats/whatsapp/test/vectors/` e
   `personal-whats-front/tests/vectors/`. Mudança incompatível = nova `ENCRYPTION_VERSION`, com
   os consumidores aceitando as duas versões até as mensagens antigas expirarem (30 dias).
4. **Ordem do deploy:** primeiro quem lê (validator, whatsapp, frontend), depois quem envia.
   Quem envia com o contrato novo antes do validator atualizar tem as mensagens rejeitadas.
5. **Testar cada projeto alterado** (`./scripts/test.sh`, `uv run pytest`, `npm test`, build) e
   reconstruir os containers com `docker compose build --no-cache` (a `main` da interface só é
   baixada de novo sem cache).
6. **Versão e commits:** bump de `src/interface_personal_whats/version.py` por semver (regra
   global). Um commit por repo, cada um só com a aprovação do usuário; mostrar a lista de repos
   alterados e as mensagens sugeridas.

Mudanças só internas (refatoração, testes, docs sem mudança de uso) não precisam ser propagadas.
