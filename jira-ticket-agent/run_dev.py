"""Local dev entrypoint: loads `.env` and runs uvicorn on APP_PORT (default 8080)."""

from __future__ import annotations

import os

from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("APP_PORT", os.environ.get("PORT", "8080")))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
