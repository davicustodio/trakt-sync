# trakt-sync

FastAPI service for movie and series identification built around Telegram, OpenRouter vision models, TMDb, OMDb, and Trakt.

## Features

- Receives Telegram webhook events for private-chat image and text messages.
- Supports `/start`, `/help`, `/whoami`, `/trakt-connect`, and `/trakt-status`.
- Supports photo caption `x-info` and photo followed by `x-info`.
- Sends immediate acknowledgement and per-stage progress updates on Telegram.
- Supports `x-save` to add the latest identified title to the requester's Trakt watchlist.
- Keeps Trakt linking per user, ready for multiuser usage.
- Falls back gracefully when TMDb does not confirm the title, instead of aborting the whole pipeline.
- Keeps the previous WhatsApp/Evolution path available as legacy compatibility while Telegram becomes the official channel.
- Receives Evolution webhook events for image and text messages.
- Unwraps WhatsApp `viewOnce` and `ephemeral` image payloads, which covers pasted screenshots/prints from mobile clients.
- Restricts `x-info` and `x-save` to owner self-chat messages in V1.
- Supports `x-info` to identify a movie or series from the latest image in the chat.
- Ignores duplicate webhook retries before re-enqueueing work.
- Falls back to inline command execution when Redis is reachable but no ARQ worker is healthy.
- Returns 2-3 likely titles instead of forcing a false positive when TMDb is ambiguous.
- Uses a deterministic free-model OpenRouter vision fallback chain.
- Escalates from free models to paid vision models when configured, and reports model/error diagnostics back to WhatsApp on `x-info` failures.
- Enriches the title with TMDb, OMDb, and Brazil streaming availability.
- Supports `x-save` to add the latest identified title to the requester's Trakt watchlist.
- Includes a small admin UI for linking a phone number to Trakt OAuth.

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.main:app --reload
```

## Worker

```bash
arq app.worker.WorkerSettings
```

## Telegram bot

- Display name: `davi-movies-shows`
- Recommended username: `davicustodio_movies_shows_bot`
- Webhook target: `https://hooks-movies-shows.duckdns.org/webhooks/telegram`

## Environment

See `.env.example`.
