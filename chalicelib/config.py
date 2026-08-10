"""Configuration: real environment first, `.env` as a local convenience.

Nothing environment-specific (addresses, bucket names, AWS profile) is hardcoded
anywhere in this repo — see `.env.example` for the full key list. In Lambda there
is no `.env`; the values come from `.chalice/config.json` environment variables.
"""

import os
from pathlib import Path

DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path = DOTENV_PATH) -> None:
    """Populate os.environ from a KEY=value file. Real env vars always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()


def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required config: {name}. Set it in .env "
            f"(copy .env.example) or in the environment."
        )
    return value
