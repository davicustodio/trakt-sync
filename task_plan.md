# Task Plan: WhatsApp media intelligence + Trakt save flow

## Goal
Produce a complete implementation plan for a Dokploy2-hosted service that receives WhatsApp media events from Evolution API, identifies movie/series posters or frames with an OpenRouter vision model, enriches the title with ratings/reviews/streaming availability in Brazil, replies on WhatsApp, and saves to Trakt watchlist on command.

## Current Phase
Phase 7

## Phases
### Phase 1: Requirements & Discovery
- [x] Understand user intent
- [x] Identify constraints and requirements
- [x] Document findings in findings.md
- **Status:** complete

### Phase 2: Research & Technical Choices
- [x] Validate viable APIs, webhook flow, and auth needs
- [x] Select LLM/provider strategy with cost-benefit rationale
- [x] Capture tradeoffs and fallback options
- **Status:** complete

### Phase 3: Plan Authoring
- [x] Write detailed architecture and delivery plan in docs/
- [x] Include security, operations, and observability guidance
- [x] Include validation and test strategy
- **Status:** complete

### Phase 4: Review & Refinement
- [x] Review assumptions and gaps
- [x] Produce targeted follow-up questions for the user
- [x] Finalize delivery summary
- **Status:** complete

### Phase 5: Implementation & Deployment
- [x] Build the FastAPI application and worker-ready codebase
- [x] Validate locally with tests
- [x] Publish the repository and deploy the application on Dokploy
- **Status:** complete

### Phase 6: Hardening & Acceptance
- [x] Enforce strict self-chat authorization for owner-only commands
- [x] Prevent duplicate webhook retries from enqueueing commands again
- [x] Handle ambiguous identification responses without false positives
- [x] Expand automated tests for webhook and worker flows
- [ ] Restore production after the OCR rollout regression
- [ ] Reconcile Dokploy/Evolution owner JID configuration
- [ ] Validate a full real-message image + `x-info` reply loop in production
- **Status:** in_progress

### Phase 7: Telegram Reformulation Plan
- [x] Capture the new Telegram-first requirements and operational constraints
- [x] Validate Telegram Bot API webhook, media, and self-chat constraints from official docs
- [x] Inspect Dokploy2-accessible environment shape for deployment planning
- [x] Write the Telegram migration plan in `docs/novo-plano-telegram.md`
- [x] Refine the plan after user answers the follow-up questions
- [x] Author a deploy checklist for Dokploy rollout
- **Status:** in_progress

## Key Questions
1. Which webhook/event shapes are available from the Evolution API instance managed inside Dokploy2?
2. Which metadata sources are most reliable for ratings, reviews, and Brazil streaming availability with acceptable cost?
3. What Trakt credentials and OAuth scopes are required for a watchlist write flow?
4. What message format best fits WhatsApp while staying within provider limits?

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Use planning files in project root | Matches required workflow for a complex multi-step task |
| Store the final detailed plan in docs/ | Matches the user's explicit delivery requirement |
| Use TMDb as the primary catalog source | It covers search, details, external IDs, reviews, and BR watch providers in one official API |
| Use OMDb as the ratings bridge | It is the simplest way to retrieve IMDb, Rotten Tomatoes, and Metacritic ratings from one API |
| Use a deterministic OpenRouter free-model cascade instead of `openrouter/free` | Deterministic fallbacks are easier to debug, measure, and tune in production |
| Implement the service as FastAPI with optional inline fallback when Redis is unavailable | Keeps the architecture queue-friendly while allowing a one-container Dokploy MVP |
| Deploy under `https://joaocat.duckdns.org/trakt-sync` instead of a new subdomain | The root host already resolves, avoiding DNS setup blocking the rollout |
| Ignore duplicate provider message IDs before command dispatch | This is the simplest way to preserve webhook idempotency for retries |
| Return ambiguity options instead of auto-selecting close TMDb matches | Better to ask for a clearer image than save or reply with a false positive |
| Unwrap `viewOnce`/`ephemeral` payloads before media extraction | Pasted screenshots from WhatsApp clients may not expose `imageMessage` at the top level |
| Only enqueue ARQ jobs when the worker health key is present | Prevents `x-info` from disappearing into Redis when only the API is running |
| Keep FastAPI/Python and swap only the messaging edge for Telegram | The business pipeline is already implemented; a channel refactor is lower-risk than a rewrite |
| Use Telegram Bot API directly instead of installing a Telegram gateway first | Official webhooks and file download APIs are sufficient for the expected workload |
| Treat Instagram clarification as an optional secondary channel, not as the core path | Telegram must become stable first; Meta onboarding should not block the migration |
| Start multiuser from day one and link Trakt per user | The user explicitly wants future scale without a second schema migration later |
| Use one editable Telegram status message plus final result messages | This keeps stage-by-stage visibility without turning the chat into noise |
| Use Postgres from the first Telegram rollout and reuse the installed Redis | The user wants multiuser Trakt from day one and provided database credentials for environment configuration |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| `rg --files` returned exit code 1 because the repo is effectively empty | 1 | Switched to `ls`/direct inspection and continued |
| Dokploy MCP deploy endpoints returned malformed JSON or did not start a build | 1 | Switched to the GitHub push webhook flow using the application's deploy token |
| Production startup initially failed behind Traefik | 2 | Added `asyncpg`, then switched deployed `DATABASE_URL` to SQLite fallback because the target environment did not expose a guaranteed Postgres host |
| Production route returned `502 Bad Gateway` after the OCR rollout | 1 | Reworked OCR initialization to lazy-load the engine so startup is no longer coupled to `rapidocr_onnxruntime` import success |
| Production `x-info` still failed after the SSL fix | 1 | Verified the WhatsApp CDN URL returned encrypted media bytes and switched media retrieval to Evolution's `getBase64FromMediaMessage` endpoint keyed by the provider message ID |

## Supplemental Notes: 2026-03-21 Telegram Reformulation
- The current codebase is structurally ready for a channel abstraction because message persistence, title identification, enrichment, and Trakt save logic are already separated from the FastAPI route layer.
- Telegram Bot API official docs confirm webhook delivery, secret-token validation, direct HTTP methods, and `sendChatAction`, so a custom Telegram bridge service is not required for the first rollout.
- Telegram bots cannot initiate a conversation with a user; the owner must start the bot first so the app can persist the private `chat_id`.
- Telegram's hosted Bot API allows file downloads up to 20 MB and covers the expected screenshot/poster use case; a local `telegram-bot-api` server is only a later optimization if larger media or special webhook routing is needed.
- Dokploy2 inspection in this session shows app/project/domain automation is available, but first-class Postgres/Redis creation is not exposed through the current `dokploy2` MCP surface, so database provisioning should be planned separately.

## Notes
- Keep web research findings out of this file and store them in findings.md.
- Re-read this plan before major decisions.
- `.env` was created locally for runtime secrets and intentionally excluded from version control.
