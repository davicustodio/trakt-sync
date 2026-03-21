# Task Plan: WhatsApp media intelligence + Trakt save flow

## Goal
Produce a complete implementation plan for a Dokploy2-hosted service that receives WhatsApp media events from Evolution API, identifies movie/series posters or frames with an OpenRouter vision model, enriches the title with ratings/reviews/streaming availability in Brazil, replies on WhatsApp, and saves to Trakt watchlist on command.

## Current Phase
Phase 5

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

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| `rg --files` returned exit code 1 because the repo is effectively empty | 1 | Switched to `ls`/direct inspection and continued |
| Dokploy MCP deploy endpoints returned malformed JSON or did not start a build | 1 | Switched to the GitHub push webhook flow using the application's deploy token |
| Production startup initially failed behind Traefik | 2 | Added `asyncpg`, then switched deployed `DATABASE_URL` to SQLite fallback because the target environment did not expose a guaranteed Postgres host |

## Notes
- Keep web research findings out of this file and store them in findings.md.
- Re-read this plan before major decisions.
- `.env` was created locally for runtime secrets and intentionally excluded from version control.
