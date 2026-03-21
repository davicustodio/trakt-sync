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
