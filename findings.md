# Findings & Decisions

## Requirements
- Use only the Evolution API instance already configured behind Dokploy2; abandon the separate Evolution API MCP path.
- Support a WhatsApp flow where the user sends or pastes an image related to a movie or series, then sends `x-info`.
- `x-info` must trigger a webhook-backed workflow that identifies the title from the image with an OpenRouter vision-capable LLM.
- The workflow must gather ratings and indexes such as IMDb, Rotten Tomatoes, TMDb, and other popular signals where feasible.
- The workflow must gather comments/reviews from APIs or data providers that legally expose them.
- The workflow must retrieve release date and streaming availability in Brazil.
- The workflow must send a formatted response back to WhatsApp.
- A follow-up `x-save` command must save the identified title into the user's Trakt watchlist.
- The deliverable for this turn is a detailed plan stored under `docs/`, including validation and testing.
- The user wants follow-up questions to improve the result.

## Research Findings
- Repository currently contains only `.git/` and an empty `docs/` directory.
- Evolution API v2 supports webhook activation via `/webhook/instance`; official docs expose `MESSAGES_UPSERT` for inbound messages and `SEND_MESSAGE` for outbound confirmation events.
- Evolution webhook docs also show `webhook_base64`, which should stay disabled in V1 unless the media-retrieval path in the installed instance requires inline payloads.
- OpenRouter's public models API, validated again with the user's API key on 2026-03-20, currently exposes 6 zero-cost models with image input.
- The current free vision-capable entries are: `openrouter/free`, `nvidia/nemotron-nano-12b-v2-vl:free`, `mistralai/mistral-small-3.1-24b-instruct:free`, `google/gemma-3-4b-it:free`, `google/gemma-3-12b-it:free`, and `google/gemma-3-27b-it:free`.
- `openrouter/free` is a random router, so it should not be the primary production fallback chain when deterministic behavior matters.
- TMDb is viable as the primary metadata source for search, details, external IDs, reviews, and watch-provider data in Brazil.
- TMDb watch-provider data is powered by JustWatch and requires attribution.
- OMDb is viable as the ratings bridge for IMDb, Rotten Tomatoes, and Metacritic, but the free plan is limited to 1,000 requests per day.
- Trakt's official contracts expose `POST /sync/watchlist`; OAuth token flows require `client_id`, `client_secret`, and `redirect_uri`.
- The user has already provided Trakt `client_id` and `client_secret` out-of-band; these values should not be persisted in repository files.
- The current V1 must reject every message that is not an owner self-chat event before persistence or queue dispatch.
- Webhook idempotency matters because Evolution can resend the same provider message ID; duplicate command events must not enqueue work twice.
- The user prefers ambiguity to be surfaced as 2-3 likely options rather than forcing a weak title match.
- WhatsApp screenshots pasted from mobile clients can arrive wrapped in `viewOnceMessage*` or `ephemeralMessage`; the webhook parser must unwrap those envelopes before looking for `imageMessage`.
- Redis availability alone is not enough to guarantee command execution; when the ARQ health key is absent, the API should execute `x-info`/`x-save` inline instead of queueing work that no worker will consume.
- The production Evolution instance `meu-whatsapp` reports `ownerJid` as `5519988343888@s.whatsapp.net`, while the Dokploy application environment still carries an older `EVOLUTION_OWNER_LID` value.
- Real end-to-end tests through Evolution proved the webhook path is live because the bot replied with a media-analysis SSL error before the SSL bypass fix.
- After introducing OCR support with an eager `RapidOCR()` import, the Dokploy route started returning `502 Bad Gateway`, which strongly suggests container startup/runtime failure inside the slim image.
- Lazy-loading the OCR engine at first use keeps the OCR fallback available without making service startup depend on the OCR runtime loading cleanly.
- Evolution accepts self-chat test payloads through `/message/sendMedia/{instance}` and `/message/sendText/{instance}`, and conversation history can be inspected through `/chat/findMessages/{instance}`.
- The `imageMessage.url` coming from WhatsApp is not reliably a directly readable image for OCR; downloading that URL can return the encrypted media blob as `application/octet-stream`.
- Evolution's official `POST /chat/getBase64FromMediaMessage/{instance}` endpoint returns the decrypted media bytes keyed by the message ID and is the correct source for image analysis.
- Local validation shows OpenRouter can identify the screenshot when fed the decrypted image bytes, so the remaining production gap is the OCR/runtime layer rather than the prompt or TMDb search path.
- The newer `rapidocr` package bundles the OCR model assets more cleanly for containerized installs and is a better first-choice backend than the deprecated `rapidocr_onnxruntime`.

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| Produce a design-first deliverable instead of code | The user explicitly asked for a complete plan and questions |
| Prefer a deterministic free-model cascade over the `openrouter/free` router | Easier to observe, benchmark, and debug in a webhook workflow |
| Rank free models by expected vision usefulness rather than by creation date | The webhook needs reliable image recognition, not novelty |
| Do not persist user secrets in docs or planning files | Repository docs should contain only env variable names, never real credentials |
| Treat duplicate provider message IDs as a no-op before worker dispatch | Prevents repeated `x-info`/`x-save` executions on webhook retries |
| Reuse the confirmed identified IDs for `x-save` | Avoids an unnecessary second TMDb lookup and reduces mismatch risk |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| No existing application code or docs to extend | Plan will define the initial architecture and delivery structure from scratch |

## Resources
- Planning skill: `/Users/davi/.agents/skills/planning-with-files/SKILL.md`
- Valyu research skill: `/Users/davi/.agents/skills/valyu-best-practices/SKILL.md`
- Evolution API webhooks: `https://doc.evolution-api.com/v2/en/configuration/webhooks`
- OpenRouter multimodal image guide: `https://openrouter.ai/docs/guides/overview/multimodal/images`
- OpenRouter public models endpoint: `https://openrouter.ai/api/v1/models`
- TMDb finding data: `https://developer.themoviedb.org/docs/finding-data`
- TMDb region support: `https://developer.themoviedb.org/docs/region-support`
- TMDb watch providers: `https://developer.themoviedb.org/reference/movie-watch-providers`
- OMDb API: `https://www.omdbapi.com/`
- OMDb API key page: `https://www.omdbapi.com/apikey.aspx`
- Trakt API repository: `https://github.com/trakt/trakt-api`
- Trakt sync contract: `https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/sync/index.ts`
- Trakt OAuth base schema: `https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/oauth/schema/request/tokenBaseSchema.ts`
- Trakt OAuth applications: `https://trakt.tv/oauth/applications`

## Visual/Browser Findings
- Evolution docs confirm the event names required for the webhook design.
- OpenRouter official docs confirm image input is supported in the standard chat format.
- The authenticated OpenRouter model list confirms the current free vision-capable set.

## Telegram Reformulation Findings - 2026-03-21
- The existing code already isolates most of the business logic from the webhook entrypoint, which makes a messaging-provider refactor practical.
- Telegram's official Bot API is HTTP-based and supports both long polling and outgoing webhooks; webhooks and `getUpdates` are mutually exclusive.
- Telegram `setWebhook` supports a `secret_token` header, which maps well to the existing webhook-secret pattern already used for Evolution.
- Telegram `sendChatAction` is explicitly intended to indicate work in progress, but the user specifically wants textual stage messages as well, so the plan should include both status text and optional chat actions.
- Telegram bots cannot start conversations with users; the user must start the bot first, after which the private `chat_id` can be stored and reused.
- Telegram hosted Bot API can download files up to 20 MB, which is likely enough for screenshots and posters; a local Bot API server is an optimization, not a base requirement.
- Telegram hosted Bot API is therefore a better first implementation target than MTProto userbots or an extra Telegram gateway service.
- The user wants the product to stay future-multiuser, so the current phone/JID-centric schema should move toward user/channel abstractions.
- The user wants provider failures such as missing TMDb matches to degrade gracefully instead of aborting the whole pipeline.
- The user also wants ambiguity handling to become an interactive clarification session and asked for Instagram as a possible clarification channel; this should be modeled as a secondary adapter so Telegram remains the official path.
- Dokploy2 inspection in this session showed a `Whatsapp-Telegram` project with an existing Evolution compose and a separate environment where `trakt-sync-fastapi` is currently healthy; this supports a staged Telegram rollout without uninstalling Evolution.
- Dokploy2 inspection also showed an existing Postgres service in another project, which is relevant because the current stable app still uses SQLite and the Telegram migration should move persistence to Postgres.
- The user confirmed on 2026-03-21 that the bot should start multiuser from the first release and that each user must connect an independent Trakt account.
- The user confirmed PostgreSQL should be used after all and provided credentials out-of-band; those secrets must stay out of repository files and go only into Dokploy environment configuration.
- The user does not yet have a Telegram bot and needs a detailed BotFather setup path documented.
- The user has only a normal Instagram account, which reinforces the decision not to make Instagram a hard dependency for the first Telegram rollout.
- For Telegram UX, the best fit is an initial acknowledgement plus edits to a status message for each step, with a separate final success or error message.
- The requested bot display name is `davi-movies-shows`, the webhook domain is `hooks-movies-shows.duckdns.org`, and the requested username `davicustodio_movies_shows` should be corrected to a BotFather-valid username ending in `bot`.
- A dedicated Dokploy deploy checklist is useful here because the next risky step is operational sequencing and secret placement, not more architectural design.
