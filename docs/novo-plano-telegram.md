# Plano Completo: Reformulacao do trakt-sync para Telegram como mensageiro oficial

## 1. Objetivo

Migrar o workflow oficial do projeto de WhatsApp + Evolution API para Telegram, preservando o pipeline ja implementado de:

1. receber imagem
2. interpretar `x-info`
3. identificar filme ou serie via LLM multimodal
4. enriquecer com TMDb, OMDb e provedores BR
5. responder ao usuario com progresso, sucesso ou erro
6. interpretar `x-save`
7. salvar o titulo identificado na watchlist do Trakt

O Evolution API nao precisa ser removido, mas deixa de ser o mensageiro oficial do produto.

## 2. Recomendacao executiva

### Melhor caminho

Usar **Telegram Bot API oficial** com **webhook HTTP** no mesmo backend FastAPI que ja existe.

### O que manter

- Python 3.12
- FastAPI
- SQLAlchemy
- Redis + worker
- integracoes OpenRouter, TMDb, OMDb e Trakt

### O que trocar

- substituir a borda de mensageria `EvolutionClient` por uma abstracao de canal
- implementar `TelegramClient` como canal oficial
- manter WhatsApp/Evolution apenas como legado, diagnostico ou fallback opcional

### O que NAO instalar inicialmente

- nao instalar um gateway extra para Telegram
- nao instalar userbot MTProto
- nao instalar um servidor local `telegram-bot-api` na primeira fase

### Quando um servidor local `telegram-bot-api` faria sentido

Somente se voce realmente precisar de pelo menos um destes pontos:

- download de arquivos acima de 20 MB
- upload de arquivos muito grandes
- webhook em IP/porta local fora das restricoes padrao
- throughput muito alto

Pela documentacao oficial, a maioria dos bots funciona bem usando a infraestrutura padrao do Telegram, e o servidor local e opcional.

## 2.1 Decisoes fechadas com base nas respostas do usuario

- a clarificacao ambigua deve nascer e terminar no **Telegram** na primeira entrega
- Instagram fica como fase posterior e **nao entra no caminho critico**
- o bot deve nascer **multiusuario**
- cada usuario tera a **propria conta Trakt**
- o projeto vai usar **PostgreSQL**
- o Redis **ja instalado** deve ser reaproveitado
- o formato de progresso recomendado sera:
  - uma mensagem inicial de ack
  - uma mensagem de status editavel para atualizar cada etapa
  - uma mensagem final separada para sucesso ou erro

Dados concretos definidos neste momento:

- nome do bot: `davi-movies-shows`
- dominio de webhook: `hooks-movies-shows.duckdns.org`
- username solicitado: `davicustodio_movies_shows`
- banco: `PostgreSQL`
- credenciais de banco recebidas fora da documentacao versionada e devem entrar apenas como variaveis de ambiente no Dokploy

Observacao importante:

- pela documentacao oficial do Telegram, o username de bot criado no `@BotFather` precisa terminar com `bot`
- por isso, `davicustodio_movies_shows` deve ser tratado como **username desejado, mas invalido para criacao padrao**
- recomendacoes validas:
  - `davicustodio_movies_shows_bot`
  - `davi_movies_shows_bot`

Essa abordagem e a melhor para Telegram porque:

- reduz ruido no chat
- continua didatica
- e um padrao bastante usado em bots transacionais

## 3. Por que Telegram e a melhor troca aqui

### Vantagens praticas sobre o fluxo atual

- API oficial, HTTP e bem documentada
- webhook mais simples que o fluxo atual com Evolution
- envio de mensagens para o proprio usuario funciona bem em chat privado com o bot
- nao depende de sessao Web WhatsApp, QR, criptografia de midia do WhatsApp ou endpoints auxiliares do Evolution para baixar imagens
- reduz a superficie de falha hoje concentrada em mensageria

### Restricao importante

Bots do Telegram **nao iniciam conversa com usuarios por conta propria**. O usuario precisa primeiro abrir o bot e enviar uma mensagem, por exemplo `/start`.

Na pratica, isso resolve o seu caso de "enviar mensagens para mim mesmo" assim:

1. voce cria o bot no `@BotFather`
2. abre o chat com o bot no seu proprio Telegram
3. envia `/start`
4. a partir dai o sistema passa a ter seu `chat_id` e pode responder para voce naquele chat

## 4. Diagnostico do estado atual do projeto

Pelo codigo atual:

- o core de negocio ja esta em `app/services.py`, `app/worker.py`, `app/clients.py` e `app/main.py`
- a mensageria esta acoplada ao `EvolutionClient`
- a persistencia ainda esta preparada para WhatsApp e telefone
- a aplicacao atual ja sabe:
  - persistir mensagens
  - localizar a ultima imagem
  - executar `x-info`
  - executar `x-save`
  - salvar o ultimo titulo identificado
  - integrar com Trakt

Conclusao:

- **nao vale reescrever o sistema**
- vale refatorar o projeto para uma arquitetura de canais, com Telegram como provider principal

## 5. Arquitetura alvo

### 5.1 Principio

Separar claramente:

- canal de mensageria
- pipeline de identificacao
- persistencia de estado
- integracoes externas

### 5.2 Novo desenho recomendado

- `api`: FastAPI
- `worker`: ARQ ou equivalente
- `postgres`: persistencia oficial
- `redis`: fila, locks e notificacoes curtas
- `telegram bot api`: usar o endpoint oficial hospedado pelo Telegram
- `instagram clarification adapter`: modulo separado e opcional para ambiguidade

Observacao:

- `Postgres` e a melhor escolha para multiusuario desde o inicio
- as credenciais devem ser mantidas apenas no ambiente do Dokploy e fora de arquivos versionados

### 5.3 Componentes

#### `MessagingProvider`

Criar uma interface unica com metodos como:

- `send_text(chat_ref, text)`
- `send_status(chat_ref, stage, detail=None)`
- `send_error(chat_ref, error_text)`
- `send_chat_action(chat_ref, action)`
- `download_image(message_ref)`
- `extract_normalized_message(payload)`

#### `TelegramClient`

Implementar o provider oficial do projeto com:

- `setWebhook`
- validacao do header `X-Telegram-Bot-Api-Secret-Token`
- download de imagem via `getFile`
- resposta com `sendMessage`
- indicacao visual curta com `sendChatAction`

#### `PipelineService`

Continua sendo o core, mas sem saber se a origem foi Telegram, WhatsApp ou outra.

#### `ClarificationService`

Novo servico para:

- armazenar sessoes ambiguas
- perguntar ao usuario qual opcao e a correta
- evoluir o contexto da conversa
- encerrar a sessao quando o titulo for confirmado

## 6. Modelo funcional novo

### 6.1 Regras de entrada

O sistema deve aceitar estes formatos:

1. foto enviada com legenda `x-info`
2. foto enviada e, em seguida, `x-info`
3. `x-save` depois de um titulo confirmado

### 6.2 Regra obrigatoria do `x-info`

Quando o usuario enviar a foto e depois `x-info`, isso deve ser interpretado como pedido de identificacao do filme ou serie usando:

- a ultima foto valida do mesmo chat
- dentro da janela TTL configurada
- respeitando a ordem temporal entre foto e comando

### 6.3 Regra obrigatoria de resposta imediata

Assim que o sistema receber um `x-info` valido, deve enviar imediatamente uma mensagem de ack, por exemplo:

`Recebi sua solicitacao. O comando x-info esta sendo processado agora.`

Essa mensagem deve sair antes do processamento pesado.

### 6.4 Regra obrigatoria de mensagens por etapa

Para cada etapa do pipeline, enviar mensagem ao usuario no Telegram informando o andamento.

Etapas minimas recomendadas:

1. `Recebi sua imagem e o x-info.`
2. `Baixando a imagem do Telegram.`
3. `Analisando a imagem com o modelo de visao.`
4. `Consultando TMDb para confirmar o titulo.`
5. `Consultando ratings e provedores.`
6. `Montando a resposta final.`
7. `Processo concluido com sucesso.`

Em caso de erro:

- enviar a etapa onde falhou
- enviar a causa resumida
- manter o erro tecnico detalhado apenas em logs internos

### 6.5 Regra obrigatoria do `x-save`

Quando o usuario enviar `x-save`, o sistema deve:

1. responder imediatamente que o comando esta sendo processado
2. informar cada etapa
3. salvar o item identificado na watchlist do Trakt
4. confirmar sucesso ou erro via Telegram

Etapas minimas do `x-save`:

1. `Recebi o x-save.`
2. `Validando o ultimo titulo identificado.`
3. `Validando sua conexao com o Trakt.`
4. `Enviando o titulo para a watchlist do Trakt.`
5. `Titulo salvo com sucesso na watchlist.`

### 6.6 Politica de tolerancia a falhas

Se um provedor falhar, o pipeline deve continuar sempre que for seguro.

Exemplo:

- se o TMDb nao encontrar o titulo, nao parar imediatamente
- tentar fallback por OMDb, Trakt search ou resultado minimo do LLM
- se ainda nao houver IDs externos para `x-save`, retornar sucesso parcial com mensagem clara

Regra:

- **falha de enriquecimento nao deve matar a identificacao basica**
- **falha de um provedor isolado nao deve interromper o restante do pipeline**

## 7. Fallbacks obrigatorios do pipeline

### 7.1 Cadeia de identificacao

1. Telegram baixa a imagem
2. OpenRouter tenta identificar titulo e tipo
3. TMDb tenta confirmar
4. OMDb tenta complementar ratings
5. Trakt search pode ser usado como fallback de catalogo
6. resposta final e montada com o que estiver disponivel

### 7.2 Se TMDb falhar

Continuar com:

- titulo sugerido pelo LLM
- ano e tipo inferidos pelo LLM
- OMDb por titulo/ano quando possivel
- tentativa posterior de match por Trakt ou por etapa de clarificacao

### 7.3 Se houver baixa confianca ou mais de uma opcao

Abrir uma sessao de clarificacao.

## 8. Ambiguidade e conversa de clarificacao

### 8.1 Regra de negocio pedida

Quando o LLM identificar mais de uma opcao para a foto, ou tiver duvidas, deve ser aberta uma conversa entre usuario e LLM ate que o sistema encontre o filme ou serie correto.

### 8.2 Recomendacao tecnica

O fluxo principal de clarificacao deve nascer no **Telegram**, porque ele sera o canal oficial.

### 8.3 Instagram no plano

Seu requisito de usar Instagram pode ser contemplado, mas deve ser tratado como **subfluxo opcional e desacoplado**, porque adiciona dependencias e restricoes externas do ecossistema Meta.

Como voce hoje tem apenas uma conta normal no Instagram, a recomendacao pratica e:

- **nao depender de Instagram na primeira entrega**
- se no futuro essa fase for realmente necessaria, preparar um onboarding proprio para Instagram com conta profissional e app Meta dedicados

Plano recomendado:

1. Fase 1: clarificacao no proprio Telegram
2. Fase 2: adicionar conector de Instagram para casos ambiguos, sem bloquear o funcionamento do produto principal

### 8.4 Como modelar a clarificacao

Criar tabela `clarification_sessions` com:

- `id`
- `user_id`
- `origin_channel`
- `fallback_channel`
- `source_message_id`
- `status`
- `candidate_payload`
- `resolved_title`
- `resolved_media_type`
- `resolved_tmdb_id`
- `resolved_imdb_id`
- `transcript_json`
- `expires_at`

### 8.5 Fluxo recomendado

1. LLM retorna 2 ou 3 opcoes provaveis ou `need_clarification=true`
2. sistema registra a sessao
3. Telegram envia:
   - `Encontrei mais de uma opcao possivel. Vou abrir uma etapa de confirmacao.`
4. Se Instagram estiver habilitado para aquele usuario:
   - iniciar conversa por Instagram
5. Se Instagram nao estiver habilitado:
   - continuar a conversa no Telegram
6. A cada resposta do usuario:
   - atualizar a sessao
   - reavaliar o match
7. Quando houver confirmacao:
   - salvar `identified_media`
   - liberar `x-save`

### 8.6 Recomendacao de produto

Nao faca a migracao principal depender do Instagram. Se o Instagram falhar, a clarificacao deve continuar no Telegram.

## 9. Persistencia e modelagem para multiusuario

### 9.1 Problema do modelo atual

Hoje o projeto esta muito orientado a:

- telefone
- JID do WhatsApp
- self-chat do dono

Isso nao serve bem para Telegram multiusuario.

### 9.2 Refatoracao recomendada

Trocar o centro do modelo para `user_profiles` e `chat_endpoints`.

### 9.3 Estruturas recomendadas

#### `user_profiles`

- `id`
- `external_user_key`
- `display_name`
- `telegram_user_id`
- `telegram_username`
- `default_channel`
- `trakt_enabled`
- `status`
- `created_at`
- `updated_at`

#### `chat_endpoints`

- `id`
- `user_profile_id`
- `channel`
- `channel_user_id`
- `channel_chat_id`
- `channel_username`
- `is_primary`
- `is_active`
- `last_seen_at`

#### `incoming_messages`

Adicionar campos:

- `channel`
- `provider_update_id`
- `provider_message_id`
- `chat_external_id`
- `user_external_id`
- `reply_to_message_id`
- `file_id`
- `file_unique_id`
- `caption`

#### `identified_media`

Adicionar:

- `channel`
- `user_profile_id`
- `chat_endpoint_id`
- `resolution_status`
- `match_source`

### 9.4 Resultado esperado

Com isso, o sistema continua disponivel para varios usuarios e fica pronto para cadastrar novos usuarios futuramente.

## 10. Como implementar Telegram de forma segura

### 10.1 Dados que voce precisa me passar ou configurar

Voce nao precisa passar seus dados pessoais completos do Telegram. Como o bot sera multiusuario, o minimo necessario para o deploy e:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- o dominio publico do webhook

Para diagnostico e testes iniciais, vale capturar tambem:

- seu `telegram_user_id`
- seu `telegram_chat_id`
- opcionalmente seu `telegram_username`

### 10.2 Como obter esses dados

#### Passo A: criar o bot

No Telegram:

1. abrir `@BotFather`
2. executar `/newbot`
3. definir nome e username do bot
4. guardar o token gerado

Guia detalhado recomendado:

1. abra o Telegram no celular ou desktop
2. busque por `@BotFather`
3. abra a conversa oficial verificada
4. envie `/start`
5. envie `/newbot`
6. quando ele pedir o nome, use algo como:
   - `Trakt Sync`
7. quando ele pedir o username, use algo como:
   - `davicustodio_movies_shows_bot`
   - `davi_movies_shows_bot`
8. o username precisa terminar com `bot` ou `_bot`
9. o `@BotFather` vai devolver:
   - o `TELEGRAM_BOT_TOKEN`
   - o link publico do bot, por exemplo `https://t.me/seu_bot`
10. guarde o token em local seguro

Para o seu caso, a recomendacao objetiva e:

- nome: `davi-movies-shows`
- username final recomendado: `davicustodio_movies_shows_bot`

Comandos adicionais recomendados no `@BotFather`:

1. `/setdescription`
   - descricao curta do bot
2. `/setabouttext`
   - texto curto do que o bot faz
3. `/setuserpic`
   - opcional, imagem do bot
4. `/setcommands`
   - registrar:
     - `start - iniciar o bot`
     - `help - ajuda`
     - `whoami - mostrar ids da sessao`
     - `x-info - identificar filme ou serie da ultima foto`
     - `x-save - salvar na watchlist do Trakt`
     - `trakt-connect - conectar sua conta Trakt`
     - `trakt-status - ver o status da sua conexao Trakt`
5. `/setjoingroups`
   - recomendacao: `Disable`

Motivo do `/setjoingroups`:

- como o fluxo sera multiusuario por chat privado e Trakt por usuario, e melhor impedir que o bot seja colocado em grupos antes de existir uma politica clara para isso

#### Passo B: ativar o bot para voce mesmo

1. abrir o chat do bot
2. enviar `/start`
3. enviar uma mensagem qualquer

Detalhe importante:

- bots do Telegram nao iniciam a conversa por conta propria
- esse `/start` e obrigatorio para que o sistema consiga responder no seu chat privado

#### Passo C: capturar `chat_id` e `user_id`

Opcoes:

1. chamar `getUpdates` antes de ligar o webhook
2. ou implementar temporariamente um endpoint `/telegram/debug/me`
3. ou criar o comando `/whoami` no proprio bot

Resposta esperada:

- `message.from.id` = seu `telegram_user_id`
- `message.chat.id` = seu `telegram_chat_id`

### 10.2.1 Validacao tecnica recomendada logo apos criar o bot

Depois de receber o token, fazer estes testes:

1. `getMe`
2. `setWebhook`
3. `getWebhookInfo`
4. enviar `/start`
5. enviar `whoami`

Exemplo de verificacao manual:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getMe"
```

Resposta esperada:

- `ok: true`
- dados basicos do bot

Exemplo para registrar webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://SEU-DOMINIO/webhooks/telegram",
    "secret_token": "SEU_SEGREDO_WEBHOOK"
  }'
```

Depois:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

O esperado e:

- webhook registrado
- `pending_update_count` baixo
- sem erro recorrente

### 10.3 Variaveis de ambiente novas

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_USE_WEBHOOK=true`
- `TELEGRAM_ENABLE_PROGRESS_MESSAGES=true`
- `TELEGRAM_ENABLE_CHAT_ACTIONS=true`
- `TELEGRAM_DEFAULT_PARSE_MODE=Markdown`

Como o bot sera multiusuario desde o inicio:

- `TELEGRAM_ALLOWED_USER_IDS` e `TELEGRAM_ALLOWED_CHAT_IDS` nao devem ser a politica definitiva
- eles podem existir apenas como trava temporaria de homologacao

### 10.4 Politica inicial de autorizacao

Como voce quer multiusuario desde o inicio:

- qualquer usuario pode abrir conversa com o bot
- cada usuario cria seu proprio perfil via `/start`
- cada usuario conecta sua propria conta Trakt
- o sistema precisa bloquear apenas abuso, flood e duplicacao

Controles recomendados:

- rate limit por usuario
- rate limit por chat
- fila por usuario
- idempotencia por `update_id`
- limite de tamanho de imagem
- limite de sessoes ambiguas simultaneas por usuario

## 11. Endpoints e comandos novos

### 11.1 Endpoints

- `POST /webhooks/telegram`
- `POST /admin/telegram/set-webhook`
- `GET /admin/telegram/webhook-info`
- `POST /admin/telegram/register-user`
- `GET /admin/telegram/me`

### 11.2 Comandos

- `/start`
- `/help`
- `/whoami`
- `x-info`
- `x-save`
- `/trakt-connect`
- `/trakt-status`

### 11.3 Regras de UX

- foto com legenda `x-info` deve executar imediatamente
- foto seguida por `x-info` deve executar usando a foto imediatamente anterior
- `x-save` deve usar o ultimo titulo confirmado do usuario
- `/start` deve criar o perfil do usuario e explicar rapidamente os comandos
- `/trakt-connect` deve iniciar o fluxo OAuth do Trakt para o usuario atual

## 12. Mudancas no codigo

### 12.1 Refatoracao principal

#### Etapa 1

Extrair interface de mensageria.

Hoje:

- `PipelineService` conhece `EvolutionClient`

Novo:

- `PipelineService` depende de `MessagingProvider`

#### Etapa 2

Criar `TelegramClient`.

Responsabilidades:

- parser do update Telegram
- download de imagem
- envio de texto
- envio de status
- envio de erro
- `sendChatAction`

#### Etapa 3

Separar `NormalizedMessage` por canal.

Campos padrao:

- `channel`
- `provider_message_id`
- `chat_id`
- `user_id`
- `message_type`
- `text_body`
- `caption`
- `media_ref`
- `received_at`

#### Etapa 4

Adaptar `MessageService` para:

- localizar ultima imagem por `chat_id` e `channel`
- localizar ultimo titulo identificado por `user_id`
- persistir progresso por job
- gerenciar onboarding multiusuario
- vincular o Trakt por usuario

#### Etapa 5

Criar `PipelineNotifier`.

Metodos:

- `notify_received`
- `notify_stage`
- `notify_success`
- `notify_partial_success`
- `notify_error`

### 12.2 Ordem tecnica recomendada

1. introduzir interface de mensageria
2. manter Evolution funcionando por compatibilidade
3. adicionar Telegram em paralelo
4. validar Telegram fim a fim
5. trocar o canal oficial para Telegram
6. deixar WhatsApp fora do caminho critico

## 13. Dokploy: o que instalar

### 13.1 Minimo necessario

No Dokploy, para Telegram, eu recomendo estes componentes:

1. `trakt-sync-api`
2. `trakt-sync-worker`
3. reuso do `redis` ja instalado
4. reuso ou conexao ao `postgres` existente

### 13.2 O que NAO e necessario para Telegram

- Evolution API para o fluxo principal
- gateway extra de Telegram
- servidor local `telegram-bot-api`

### 13.3 Recomendacao concreta

Usar `postgres` ja nesta fase.

Motivos:

- multiusuario real desde o inicio
- menos risco de lock contention com worker + webhook
- melhor base para Trakt por usuario e sessoes de clarificacao
- evita uma segunda migracao estrutural logo depois da troca de canal

### 13.4 Observacao com base no ambiente atual

O MCP do Dokploy2 neste ambiente mostra:

- um projeto `Whatsapp-Telegram`
- uma compose do Evolution ja existente
- uma aplicacao `trakt-sync-api`
- um Postgres existente reaproveitavel
- Redis ja instalado no ambiente

Conclusao pratica:

- preserve o Evolution instalado
- mova o fluxo oficial para Telegram
- reaproveite o Redis ja instalado
- conecte o app ao Postgres existente desde a primeira entrega

## 14. Como usar o MCP `dokploy2` no plano

### 14.1 O que o MCP ajuda a automatizar

No MCP `dokploy2` disponivel nesta sessao, da para automatizar:

- leitura de projetos
- leitura de ambientes
- leitura de aplicacoes
- criacao e atualizacao de aplicacoes
- configuracao de dominio
- deploy e redeploy
- leitura de status de deploy

### 14.2 O que precisa de atencao

Neste conjunto de ferramentas nao ha uma operacao dedicada para criar Postgres ou Redis via `dokploy2`.

Entao o plano operacional deve ser:

1. usar `dokploy2` para app, dominio e deploy
2. usar Dokploy UI, compose existente ou outro fluxo para banco e redis

### 14.3 Sequencia operacional recomendada com MCP

1. listar projetos e ambientes com `project-all`
2. escolher o ambiente de producao alvo
3. criar ou atualizar a app `trakt-sync-telegram-api`
4. criar ou atualizar a app `trakt-sync-telegram-worker`
5. conectar a app ao Postgres existente
6. conectar a app ao Redis ja instalado
7. configurar dominio HTTPS publico
8. subir env vars do Telegram, Trakt, OpenRouter, TMDb, OMDb e banco
9. executar deploy
10. consultar status e logs de deployment
11. registrar o webhook do Telegram
12. validar `getWebhookInfo`

### 14.4 Estrategia de rollout

- `blue/green` simples por nova app ou novo dominio
- primeiro homologar Telegram sem desligar o WhatsApp
- depois mover o webhook oficial

## 15. Webhook e seguranca

### 15.1 Regras

- usar `setWebhook`
- validar `X-Telegram-Bot-Api-Secret-Token`
- deduplicar por `update_id` e `message_id`
- nao logar tokens
- aplicar rate limiting por usuario e por chat

### 15.2 Idempotencia

Persistir:

- `update_id`
- `message_id`
- `chat_id`
- `user_id`

Se repetir update:

- responder `200`
- nao reprocessar

## 16. Fluxo detalhado do `x-info`

1. Telegram envia update com foto ou texto
2. API normaliza e persiste a mensagem
3. Se a entrada for foto com legenda `x-info`:
   - dispara o pipeline imediatamente
4. Se a entrada for texto `x-info`:
   - busca a ultima foto valida do mesmo chat/usuario
5. API envia ack imediato no Telegram
6. API ou worker publica/consome job
7. worker envia mensagem `Baixando a imagem`
8. worker faz `getFile` e download
9. worker envia `Analisando a imagem`
10. OpenRouter identifica candidatos
11. worker envia `Consultando TMDb`
12. TMDb tenta confirmar
13. se TMDb falhar:
    - continuar com fallback
14. worker envia `Consultando ratings e provedores`
15. OMDb/TMDb/Trakt complementam
16. se houver ambiguidade:
    - abrir sessao de clarificacao
17. se houver sucesso:
    - salvar `identified_media`
    - enviar mensagem final formatada

## 17. Fluxo detalhado do `x-save`

1. Telegram recebe `x-save`
2. API persiste a mensagem
3. API envia ack imediato
4. worker envia `Validando ultimo titulo identificado`
5. recupera o ultimo `identified_media`
6. worker envia `Validando conexao Trakt`
7. renova token se necessario
8. worker envia `Salvando na watchlist`
9. chama `POST /sync/watchlist`
10. envia sucesso ou erro final

## 18. Formato das mensagens para o usuario

### 18.1 Progresso

Padrao recomendado:

- enviar uma mensagem inicial de ack
- criar uma mensagem de status
- editar essa mesma mensagem a cada etapa
- enviar uma mensagem final separada de sucesso ou erro

Exemplo de ack:

`Recebi sua solicitacao. O x-info esta em processamento.`

Exemplo de status editavel:

`[x-info] Etapa 3/6: analisando a imagem com o modelo de visao.`

### 18.2 Erro

Exemplo:

`[x-info] Falha na etapa "consultando TMDb". O pipeline vai continuar com fallback.`

### 18.3 Sucesso parcial

Exemplo:

`Identifiquei o titulo, mas nao consegui confirmar dados do TMDb. Vou te entregar o melhor resultado disponivel.`

### 18.4 Por que esse formato e o melhor aqui

- e popular em bots de suporte e automacao
- reduz spam visual
- deixa claro que o job ainda esta vivo
- facilita leitura em mobile
- continua atendendo a exigencia de informar cada etapa

## 19. Workflow completo de testes

### 19.1 Objetivo

Diagnosticar e validar o pipeline inteiro, nao apenas testes unitarios.

### 19.2 Camadas de teste

#### Unitarios

- parser de update Telegram
- deduplicacao por `update_id`
- selecao da ultima imagem
- binding foto + `x-info`
- recuperacao do ultimo identificado para `x-save`
- formatter de mensagens de progresso
- formatter de erros

#### Contrato

- `setWebhook`
- `getWebhookInfo`
- `getFile`
- `sendMessage`
- `sendChatAction`
- OpenRouter
- TMDb
- OMDb
- Trakt `/sync/watchlist`

#### Integracao local

- foto com legenda `x-info`
- foto seguida de `x-info`
- `x-info` sem foto recente
- `x-save` apos identificacao
- `x-save` sem contexto
- onboarding `/start`
- `/whoami`
- `/trakt-connect`
- token Trakt expirado
- TMDb sem match
- OMDb indisponivel
- ambiguidade com abertura de sessao de clarificacao

#### Integracao em staging

- webhook real do Telegram
- download real de imagem
- resposta real do bot no proprio chat
- cadeia completa `foto -> x-info -> resposta`
- cadeia completa `x-save -> Trakt`

#### E2E de regressao

- 20 posters
- 10 frames de filmes
- 10 frames de series
- 5 imagens ambiguas
- 5 imagens com TMDb sem match claro

### 19.3 Casos obrigatorios de diagnostico

1. webhook chega mas o ack nao e enviado
2. ack enviado, mas job nao e executado
3. download da imagem falha
4. OpenRouter falha em todos os modelos
5. TMDb falha, mas o pipeline continua
6. `identified_media` nao salva
7. `x-save` nao encontra contexto
8. Trakt retorna erro de autenticacao
9. progresso nao e enviado ao usuario
10. ambiguidade nao abre sessao

### 19.4 Instrumentacao

Registrar por job:

- `correlation_id`
- `channel`
- `chat_id`
- `user_id`
- `command`
- `stage`
- `status`
- `latency_ms`
- `provider`
- `fallback_count`
- `final_resolution`

### 19.5 Criterios de aceite

- o bot responde para voce no Telegram privado logo apos `/start`
- foto + `x-info` funciona
- foto seguida por `x-info` funciona
- `x-save` salva corretamente no Trakt
- cada etapa envia mensagem de progresso
- erros sao informados ao usuario
- falha de TMDb nao interrompe o fluxo inteiro
- o sistema suporta mais de um usuario no modelo de dados
- o WhatsApp deixa de ser necessario para o caminho critico

## 20. Roadmap por fases

### Fase A: Preparacao

- criar bot no Telegram
- configurar webhook
- capturar `user_id` e `chat_id`
- criar env vars

### Fase B: Refatoracao de arquitetura

- introduzir `MessagingProvider`
- isolar Evolution
- criar `TelegramClient`

### Fase C: Persistencia correta

- ajustar modelo para multiusuario e multiplos canais
- conectar ao Postgres existente
- criar migracoes e configuracao de ambiente

### Fase D: Pipeline Telegram

- receber foto
- suportar `x-info`
- suportar `x-save`
- enviar progresso e erros em todas as etapas
- suportar onboarding `/start`
- suportar vinculacao Trakt por usuario

### Fase E: Fallbacks e resiliencia

- continuar sem TMDb
- deduplicacao forte
- retries seguros
- sucesso parcial

### Fase F: Clarificacao

- sessao de clarificacao no Telegram
- reavaliar somente depois a necessidade de Instagram

### Fase G: Validacao e corte oficial

- staging real
- E2E real
- Telegram vira canal oficial
- WhatsApp sai do caminho critico

## 21. Dados que faltam para executar a implantacao

1. username final do bot no Telegram que realmente sera criado no `@BotFather`
2. confirmacao do dominio publico `hooks-movies-shows.duckdns.org`
3. string `DATABASE_URL` final que sera usada no Dokploy
4. `TRAKT_CLIENT_ID` e `TRAKT_CLIENT_SECRET` definitivos de producao

## 22. Perguntas finais de refinamento

1. Voce quer que eu proponha agora nomes e usernames concretos para o bot?
2. Voce quer que eu transforme este plano em um checklist operacional de deploy no Dokploy?
3. Voce quer que eu prepare em seguida o plano tecnico de implementacao por arquivos e migracoes?

## 23. Fontes

- Telegram Bot API: [https://core.telegram.org/bots/api](https://core.telegram.org/bots/api)
- Telegram Bots FAQ: [https://core.telegram.org/bots/faq](https://core.telegram.org/bots/faq)
- Telegram bot intro e restricoes de conversa: [https://core.telegram.org/bots](https://core.telegram.org/bots)
- Telegram BotFather / criacao de bot: [https://core.telegram.org/bots/features](https://core.telegram.org/bots/features)
- Trakt API repository: [https://github.com/trakt/trakt-api](https://github.com/trakt/trakt-api)
- TMDb docs: [https://developer.themoviedb.org/docs/finding-data](https://developer.themoviedb.org/docs/finding-data)
- OMDb API: [https://www.omdbapi.com/](https://www.omdbapi.com/)

## 24. Resumo da decisao

Se o objetivo e fazer o projeto voltar a funcionar com menos atrito e com resposta confiavel para voce mesmo, a melhor decisao e:

- **Telegram Bot API oficial**
- **FastAPI + worker + Postgres + Redis**
- **Telegram como canal oficial**
- **Evolution mantido, mas fora do fluxo principal**
- **clarificacao ambigua primeiro no Telegram, sem depender de Instagram**
- **multiusuario desde o inicio**
- **Trakt por usuario**
