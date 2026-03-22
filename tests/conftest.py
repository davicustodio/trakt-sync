from __future__ import annotations

import os


# Tests must not inherit the deployment database from the local .env.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_trakt_sync.db"
