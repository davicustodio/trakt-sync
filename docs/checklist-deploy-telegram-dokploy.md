# Checklist Operacional: Deploy do Telegram no Dokploy

## 1. Objetivo

Executar o rollout do `trakt-sync` com Telegram como canal oficial, usando:

- `FastAPI`
- `worker`
- `PostgreSQL`
- `Redis` ja instalado
- webhook em `hooks-movies-shows.duckdns.org`

Este checklist assume que o plano principal ja foi aprovado em [novo-plano-telegram.md](/Users/davi/development/trakt-sync/docs/novo-plano-telegram.md).

## 2. Decisoes fechadas

- nome do bot: `davi-movies-shows`
- username recomendado do bot: `davicustodio_movies_shows_bot`
- webhook publico: `https://hooks-movies-shows.duckdns.org/webhooks/telegram`
- banco: `PostgreSQL`
- fila/cache: `Redis` ja instalado
- canal oficial: `Telegram`
- multiusuario: `sim`
- Trakt por usuario: `sim`

## 3. Pre-checks

Antes de mexer no Dokploy:

1. confirmar que `hooks-movies-shows.duckdns.org` responde para o servidor do Dokploy
2. confirmar que Traefik ou proxy do Dokploy consegue expor HTTPS valido
3. confirmar string final de `DATABASE_URL`
4. confirmar `REDIS_URL`
5. confirmar `TRAKT_CLIENT_ID` e `TRAKT_CLIENT_SECRET`
6. confirmar `OPENROUTER_API_KEY`, `TMDB_API_TOKEN` e `OMDB_API_KEY`

## 4. Criacao do bot no Telegram

### 4.1 Criar no `@BotFather`

1. abrir `@BotFather`
2. enviar `/start`
3. enviar `/newbot`
4. informar nome:
   - `davi-movies-shows`
5. informar username:
   - `davicustodio_movies_shows_bot`
6. guardar o token retornado

### 4.2 Comandos recomendados no `@BotFather`

1. `/setdescription`
2. `/setabouttext`
3. `/setuserpic`
4. `/setcommands`
5. `/setjoingroups` -> `Disable`

Comandos sugeridos:

- `start - iniciar o bot`
- `help - ajuda`
- `whoami - mostrar ids da sessao`
- `x-info - identificar filme ou serie da ultima foto`
- `x-save - salvar na watchlist do Trakt`
- `trakt-connect - conectar sua conta Trakt`
- `trakt-status - ver o status da conta Trakt`

## 5. Variaveis de ambiente de producao

## 5.1 Ja existentes

- `APP_ENV=production`
- `APP_BASE_URL=https://hooks-movies-shows.duckdns.org`
- `LOG_LEVEL=INFO`
- `ADMIN_SHARED_SECRET=<gerar-valor-forte>`
- `OPENROUTER_API_KEY=<secret>`
- `TMDB_API_TOKEN=<secret>`
- `TMDB_REGION=BR`
- `TMDB_LANGUAGE=pt-BR`
- `OMDB_API_KEY=<secret>`
- `TRAKT_CLIENT_ID=<secret>`
- `TRAKT_CLIENT_SECRET=<secret>`
- `TRAKT_REDIRECT_URI=https://hooks-movies-shows.duckdns.org/auth/trakt/callback`

## 5.2 Banco e fila

- `DATABASE_URL=postgresql+asyncpg://<usuario>:<senha>@<host>:<porta>/<database>`
- `REDIS_URL=redis://<host>:<porta>/<db>`

## 5.3 Novas para Telegram

- `TELEGRAM_BOT_NAME=davi-movies-shows`
- `TELEGRAM_BOT_USERNAME=davicustodio_movies_shows_bot`
- `TELEGRAM_BOT_TOKEN=<secret>`
- `TELEGRAM_WEBHOOK_SECRET=<secret>`
- `TELEGRAM_WEBHOOK_URL=https://hooks-movies-shows.duckdns.org/webhooks/telegram`
- `TELEGRAM_ENABLE_PROGRESS_MESSAGES=true`
- `TELEGRAM_ENABLE_CHAT_ACTIONS=true`
- `TELEGRAM_DEFAULT_PARSE_MODE=Markdown`
- `MULTIUSER_MODE=true`

## 5.4 Legadas que devem sair do caminho critico

Se o deploy do Telegram ja estiver pronto, o plano e:

- manter `EVOLUTION_*` apenas enquanto o fallback legado existir
- nao depender de `SELF_CHAT_ONLY_MODE` como regra principal do produto

## 6. Aplicacoes no Dokploy

## 6.1 API

Aplicacao:

- `trakt-sync-telegram-api`

Responsabilidades:

- receber webhook do Telegram
- registrar mensagens
- disparar jobs
- expor admin e callback Trakt

## 6.2 Worker

Aplicacao:

- `trakt-sync-telegram-worker`

Responsabilidades:

- processar `x-info`
- processar `x-save`
- enviar progresso
- abrir sessao de clarificacao

## 6.3 Dependencias

- Postgres existente
- Redis existente

## 7. Sequencia de deploy no Dokploy

1. criar ou atualizar a app `trakt-sync-telegram-api`
2. apontar para o repositorio e branch corretos
3. configurar build `Dockerfile`
4. injetar todas as env vars
5. conectar `DATABASE_URL`
6. conectar `REDIS_URL`
7. configurar dominio publico `hooks-movies-shows.duckdns.org`
8. criar ou atualizar a app `trakt-sync-telegram-worker`
9. repetir env vars do worker
10. executar deploy da API
11. executar deploy do worker
12. validar healthcheck da API
13. validar logs da API e do worker

## 8. Registro do webhook Telegram

Depois que a API estiver no ar:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://hooks-movies-shows.duckdns.org/webhooks/telegram",
    "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"
  }'
```

Validar:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

Esperado:

- `ok: true`
- `url` correto
- `pending_update_count` baixo
- nenhum erro recorrente

## 9. Checklist de validacao

## 9.1 Infra

- dominio responde em HTTPS
- API responde `200` em `/health`
- API responde `200` em `/ready`
- worker sobe sem erro
- Postgres conecta sem erro
- Redis conecta sem erro

## 9.2 Telegram

- `getMe` retorna `ok: true`
- webhook registrado corretamente
- voce consegue abrir o bot
- `/start` funciona
- `/whoami` retorna `chat_id` e `user_id`

## 9.3 Fluxo funcional

- enviar foto com legenda `x-info`
- enviar foto e depois `x-info`
- receber ack imediato
- ver a mensagem de status sendo atualizada
- receber resposta final
- executar `x-save`
- conectar Trakt por usuario
- salvar na watchlist correta

## 9.4 Casos de erro

- `x-info` sem foto recente
- imagem invalida
- falha do OpenRouter
- falha do TMDb com continuidade do pipeline
- usuario sem conta Trakt vinculada
- token Trakt expirado

## 10. Gatilhos de rollback

Fazer rollback se:

- webhook nao chega
- ack imediato nao sai
- worker nao processa jobs
- progresso nao aparece
- `x-save` nao grava na watchlist
- erros de banco ou fila travam o fluxo

## 11. O que NAO colocar no repositorio

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- senha do `PostgreSQL`
- `OPENROUTER_API_KEY`
- `TRAKT_CLIENT_SECRET`
- qualquer `DATABASE_URL` real com segredo

## 12. Proximo passo recomendado

Depois deste checklist, a sequencia mais util e:

1. implementar a refatoracao do provider de mensageria
2. adicionar `TelegramClient`
3. ajustar o schema para multiusuario e Trakt por usuario
4. ligar o deploy no Dokploy
