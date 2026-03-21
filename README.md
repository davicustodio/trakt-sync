# trakt-sync

FastAPI service for a WhatsApp media assistant built around Evolution API, OpenRouter vision models, TMDb, OMDb, and Trakt.

## Features

- Receives Evolution webhook events for image and text messages.
- Restricts `x-info` and `x-save` to owner self-chat messages in V1.
- Supports `x-info` to identify a movie or series from the latest image in the chat.
- Ignores duplicate webhook retries before re-enqueueing work.
- Returns 2-3 likely titles instead of forcing a false positive when TMDb is ambiguous.
- Uses a deterministic free-model OpenRouter vision fallback chain.
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

## Environment

See `.env.example`.
