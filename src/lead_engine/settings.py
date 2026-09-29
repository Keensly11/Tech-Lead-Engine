"""Environment settings and YAML config loading."""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
OUT_DIR = ROOT / "out"

load_dotenv(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@lru_cache
def load_config(name: str) -> dict:
    """Load config/<name>.yaml."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _database_url() -> str:
    url = env("DATABASE_URL", "sqlite:///lead_engine.db")
    # Relative SQLite paths resolve against the project root, not the caller's cwd,
    # so the CLI and the MCP server (launched by Claude Desktop from elsewhere) share one DB.
    prefix = "sqlite:///"
    if url.startswith(prefix) and url != "sqlite:///:memory:":
        path = Path(url[len(prefix):])
        if not path.is_absolute():
            return f"{prefix}{(ROOT / path).as_posix()}"
    return url


DATABASE_URL = _database_url()
