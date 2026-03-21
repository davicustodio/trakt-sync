# Plano Completo: WhatsApp + Evolution + OpenRouter + Trakt

## 1. Objetivo

Criar um servico no Dokploy2 que use a instancia ja existente do Evolution API para:

1. receber imagem no WhatsApp
2. interpretar `x-info`
3. identificar o filme ou serie na imagem
4. agregar ratings, reviews, lancamento e streaming no Brasil
5. responder no WhatsApp
6. interpretar `x-save`
7. salvar o titulo na watchlist do Trakt

## 2. Escopo de V1

### Incluido

- webhook inbound do Evolution
- comandos `x-info` e `x-save`
- reconhecimento por imagem via OpenRouter
- enriquecimento via TMDb + OMDb
- streaming BR via TMDb watch providers
- watchlist Trakt
- logs, idempotencia, retries, validacao e testes
- modo estrito inicial: somente mensagens enviadas pelo dono da instancia para o proprio WhatsApp disparam automacoes

### Fora de V1

- scraping de IMDb, Rotten Tomatoes ou Letterboxd
- execucao de comandos para numeros externos no modo inicial
- painel administrativo completo
- OCR dedicado separado do modelo multimodal

## 2.1 Regra critica de acionamento

No estado atual do produto, `x-info` e `x-save` **so podem ser executados** quando:

- a mensagem foi enviada pelo proprio dono da instancia Evolution
- a mensagem foi enviada para o proprio chat do dono
- o numero remetente coincide com o numero dono configurado em `EVOLUTION_OWNER_PHONE`

Se a mensagem vier de outro numero de WhatsApp, o servico deve ignorar totalmente o evento e nao persistir nem disparar automacao.

### Preparacao para futuro multiusuario

A base continua preparada para evoluir para multiusuario, porque:

- existe uma entidade de telefone/perfil
- a vinculacao Trakt ja e modelada por telefone
- o comportamento estrito fica controlado por `SELF_CHAT_ONLY_MODE=true`

Quando voce quiser abrir o servico para terceiros, basta desligar esse modo e endurecer a camada de autorizacao por numero.

## 3. Stack recomendada

- `webhook-api`: Python 3.12 + FastAPI
- `worker`: Python 3.12 + ARQ ou Dramatiq
- `postgres`: persistencia e idempotencia
- `redis`: fila e locks curtos

### Escolha recomendada

Escolha **Python + FastAPI**.

Motivos:

- o problema e centrado em integracao com LLM, parsing de JSON, heuristicas de matching e IO externo
- Python oferece menos atrito para esse tipo de fluxo do que Node neste projeto
- FastAPI entrega tipagem forte com Pydantic, boa ergonomia para webhooks e callback OAuth
- a mesma base em Python pode servir tanto para a API quanto para o worker
- como o uso inicial parece pessoal ou de baixo volume, a performance de Python e mais do que suficiente

### O que eu usaria na pratica

- `fastapi` para a API
- `uvicorn` para servir HTTP
- `httpx` para OpenRouter, TMDb, OMDb, Trakt e Evolution
- `pydantic-settings` para configuracao
- `sqlalchemy` + `alembic` para banco
- `redis` + `arq` para jobs assincronos
- `structlog` ou logging JSON simples para observabilidade

### O que eu nao recomendo

- nao usar `FastAPI BackgroundTasks` para o fluxo pesado de `x-info`
- nao fazer reconhecimento, TMDb, OMDb e Trakt dentro do request do webhook

O webhook deve:

1. validar o evento
2. persistir o payload e o estado minimo
3. publicar um job no Redis
4. responder `200` rapidamente

## 4. Fontes de dados

### TMDb

Usar como fonte primaria para:

- busca e normalizacao do titulo
- detalhes do filme/serie
- overview
- data de lancamento
- external IDs
- reviews
- streaming no Brasil

### OMDb

Usar como fonte secundaria para:

- IMDb rating
- Rotten Tomatoes
- Metacritic

### Trakt

Usar apenas para:

- salvar item normalizado na watchlist

## 5. Estrategia OpenRouter

### Regra principal

Usar somente modelos free com visao e fallback deterministico.

### Cadeia recomendada

1. `mistralai/mistral-small-3.1-24b-instruct:free`
2. `google/gemma-3-27b-it:free`
3. `nvidia/nemotron-nano-12b-v2-vl:free`
4. `google/gemma-3-12b-it:free`
5. `google/gemma-3-4b-it:free`

### Nao usar como primeira escolha

- `openrouter/free`

Motivo:

- ele nao e deterministico
- dificulta benchmark
- dificulta reproducao de bugs
- dificulta medir qual modelo acertou ou errou

Detalhe completo em [openrouter-free-vision-fallback.md](/Users/davi/development/trakt-sync/docs/openrouter-free-vision-fallback.md).

## 6. Fluxo funcional

### 6.1 Imagem recebida

1. Evolution envia `MESSAGES_UPSERT`.
2. O servico valida se a mensagem veio do dono para o proprio chat.
3. So depois disso ele identifica se a mensagem contem imagem.
4. O servico persiste a mensagem e marca como `pending_info`.
5. O webhook responde rapido com `200`.

### 6.2 `x-info`

1. Evolution envia novo `MESSAGES_UPSERT` com texto `x-info`.
2. O servico valida novamente a regra de self-chat.
3. O servico busca a ultima imagem valida do mesmo chat.
3. Se nao houver imagem recente, responde pedindo que a imagem seja reenviada.
4. Se houver:
   - cria um job
   - tenta o primeiro modelo da cadeia
   - se falhar, avanca para o seguinte
5. O resultado do modelo e convertido para candidato normalizado.
6. O TMDb faz a busca e confirma o titulo mais provavel.
7. O servico coleta dados adicionais de TMDb e OMDb.
8. O servico envia resposta formatada no WhatsApp.
9. O servico salva esse titulo como `last_identified_media` do chat.

### 6.3 `x-save`

1. Evolution envia `MESSAGES_UPSERT` com texto `x-save`.
2. O servico valida novamente a regra de self-chat.
3. O servico procura o ultimo titulo confirmado no mesmo chat.
3. Se nao existir, responde pedindo `x-info`.
4. Se existir:
   - valida token do Trakt
   - faz `POST /sync/watchlist`
   - responde com sucesso ou erro amigavel

## 7. Regras de identificacao

### Prompt

Exigir JSON estrito:

```json
{
  "detected_title": "string | null",
  "media_type": "movie | series | unknown",
  "year": 2023,
  "confidence": 0.0,
  "alt_titles": ["..."],
  "visible_text": ["..."],
  "need_clarification": false
}
```

### Quando trocar de modelo

- timeout
- erro HTTP
- JSON invalido
- `detected_title` vazio
- `confidence < 0.80`
- `need_clarification = true`
- TMDb nao conseguiu confirmar o candidato

### Quando pedir nova imagem

- todos os modelos falharam
- TMDb retornou ambiguidade forte
- a imagem e claramente ruidosa ou incompleta

## 8. Formato da resposta no WhatsApp

```text
TITULO (ANO)
Tipo: Filme | Serie
Lancamento: DD/MM/AAAA

Notas
- IMDb: 8.1/10
- Rotten Tomatoes: 92%
- Metacritic: 78/100
- TMDb: 7.9/10

Onde assistir no Brasil
- Netflix (assinatura)
- Prime Video (aluguel)

Resumo
Texto curto do TMDb.

Reviews
1. Review curta 1
2. Review curta 2

Comando
- x-save
```

### Regras de compactacao

- overview curto
- no maximo 2 ou 3 reviews
- exibir apenas ratings disponiveis
- se nao houver provider BR, informar isso claramente

## 9. Persistencia minima

### `incoming_messages`

- `provider_message_id`
- `chat_jid`
- `message_type`
- `text_body`
- `media_ref`
- `raw_payload_json`
- `received_at`

### `chat_state`

- `chat_jid`
- `last_image_message_id`
- `last_identified_media_id`
- `updated_at`

### `identified_media`

- `chat_jid`
- `source_message_id`
- `media_type`
- `tmdb_id`
- `imdb_id`
- `title`
- `year`
- `confidence`
- `normalized_payload_json`

### `trakt_tokens`

- `access_token`
- `refresh_token`
- `expires_at`

## 10. Endpoints do servico

- `POST /webhooks/evolution/messages`
- `GET /health`
- `GET /ready`
- `POST /admin/trakt/bootstrap`
- `GET /admin/trakt/status`

## 11. Configuracao no Dokploy2

### Recomendacao

- manter `webhook-api`, `worker`, `postgres` e `redis` no mesmo projeto/ambiente do Evolution
- preferir comunicacao privada pela rede interna
- nao expor mais do que o necessario
- rodar API e worker como processos separados, mesmo compartilhando a mesma base Python

### Eventos do Evolution

Configurar no minimo:

- `MESSAGES_UPSERT`
- `SEND_MESSAGE`

Configuracao desejada:

- `webhook_base64=false`
- `webhook_by_events=false` na V1 para simplificar

## 12. Variaveis de ambiente

### Evolution

- `EVOLUTION_BASE_URL`
- `EVOLUTION_API_KEY`
- `EVOLUTION_INSTANCE`

### OpenRouter

- `OPENROUTER_API_KEY`
- `OPENROUTER_VISION_MODELS`
- `OPENROUTER_EMERGENCY_ROUTER=openrouter/free`

### TMDb / OMDb

- `TMDB_API_TOKEN`
- `TMDB_REGION=BR`
- `TMDB_LANGUAGE=pt-BR`
- `OMDB_API_KEY`

### Trakt

- `TRAKT_CLIENT_ID`
- `TRAKT_CLIENT_SECRET`
- `TRAKT_REDIRECT_URI`
- `TRAKT_ACCESS_TOKEN`
- `TRAKT_REFRESH_TOKEN`
- `TRAKT_TOKEN_EXPIRES_AT`

Observacao:

- `TRAKT_CLIENT_ID` e `TRAKT_CLIENT_SECRET` ja foram fornecidos pelo usuario fora do repositorio.
- Nao gravar esses valores em arquivo versionado.
- Ainda falta definir `TRAKT_REDIRECT_URI` e obter os tokens iniciais.

## 13. O que falta para autenticar no Trakt

Ja disponivel:

- `client_id`
- `client_secret`

Ainda necessario:

- `redirect_uri`
- fluxo OAuth inicial
- `access_token`
- `refresh_token`
- estrategia de refresh

Recomendacao:

- fazer o bootstrap por um endpoint admin ou script de setup, nao pelo WhatsApp na V1

## 14. Seguranca

- validar origem/instancia do webhook
- se possivel usar segredo no webhook
- se nao for possivel, usar path aleatorio + rede interna + proxy
- nunca logar segredos
- criptografar tokens do Trakt em repouso se possivel
- deduplicar mensagens por `provider_message_id`

## 15. Observabilidade

### Logs

- `correlation_id`
- `chat_jid`
- `provider_message_id`
- `command`
- `selected_model`
- `fallback_count`
- `status`
- `latency_ms`

### Metricas

- taxa de sucesso de `x-info`
- taxa de sucesso de `x-save`
- distribuicao por modelo escolhido
- taxa de fallback por etapa
- tempo medio por provider
- erros por provider

## 16. Plano de implementacao

### Fase A

- criar API FastAPI e worker Python
- provisionar Postgres e Redis
- criar schema inicial
- registrar webhook no Evolution

### Fase B

- persistir imagens e `chat_state`
- implementar regra de ultima imagem valida
- implementar fila

### Fase C

- integrar OpenRouter
- implementar parser JSON estrito
- implementar cascade de modelos
- integrar TMDb para confirmacao

### Fase D

- integrar OMDb
- formatar resposta WhatsApp
- integrar envio de resposta pelo Evolution

### Fase E

- integrar Trakt watchlist
- refresh token
- endpoint admin de bootstrap

### Fase F

- hardening
- metricas
- retries
- testes E2E

## 17. Validacao e testes

### Unitarios

- parser de comando
- seletor da ultima imagem
- regras de fallback
- formatter de resposta
- refresh token do Trakt

### Contrato

- payload `MESSAGES_UPSERT`
- resposta OpenRouter
- TMDb search/details/reviews/watch providers
- OMDb ratings
- Trakt `/sync/watchlist`

### Integracao

- imagem -> `x-info` -> resposta
- `x-info` sem imagem -> erro amigavel
- `x-save` apos sucesso -> salvo
- `x-save` sem contexto -> erro amigavel
- timeout de modelo -> troca para o proximo

### Benchmark de reconhecimento

- 20 posters
- 10 frames de filmes
- 10 frames de series
- 5 ambiguos

Metas iniciais:

- top-1 >= 85% em posters
- top-1 >= 70% em frames
- falso positivo critico <= 3%

## 18. Criterios de aceite

- `x-info` responde corretamente em ate 20s em condicoes normais
- ratings aparecem quando existirem
- providers BR aparecem quando existirem
- `x-save` salva na watchlist correta
- o sistema nao duplica processamento no retry do webhook
- logs permitem rastrear qualquer falha

## 19. Perguntas para refinar

1. Esse bot sera usado so por voce, ou por mais contatos?
2. A watchlist do Trakt sera sempre a mesma conta pessoal?
3. Voce prefere bootstrap do Trakt por endpoint admin ou por script local?
4. Quando houver ambiguidade, voce quer confirmacao com opcoes ou prefere pedir nova imagem?
5. A resposta deve ser sempre em PT-BR, mesmo quando reviews vierem em ingles?
6. Voce quer incluir runtime, generos e classificacao indicativa na mensagem final?
7. O novo servico ficara na mesma rede privada do Evolution dentro do Dokploy2?

## 20. Fontes

- Evolution webhooks: [https://doc.evolution-api.com/v2/en/configuration/webhooks](https://doc.evolution-api.com/v2/en/configuration/webhooks)
- OpenRouter image guide: [https://openrouter.ai/docs/guides/overview/multimodal/images](https://openrouter.ai/docs/guides/overview/multimodal/images)
- OpenRouter models endpoint: [https://openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models)
- TMDb docs: [https://developer.themoviedb.org/docs/finding-data](https://developer.themoviedb.org/docs/finding-data)
- TMDb watch providers: [https://developer.themoviedb.org/reference/movie-watch-providers](https://developer.themoviedb.org/reference/movie-watch-providers)
- OMDb: [https://www.omdbapi.com/](https://www.omdbapi.com/)
- Trakt API repo: [https://github.com/trakt/trakt-api](https://github.com/trakt/trakt-api)
- Trakt OAuth apps: [https://trakt.tv/oauth/applications](https://trakt.tv/oauth/applications)
