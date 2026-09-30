"""Local LLM calls via Ollama with schema-constrained JSON output."""

import json
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from lead_engine.settings import env

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Generous: the first call also loads the model into GPU memory.
TIMEOUT = 300


def llm_schema(model: type[BaseModel]) -> dict:
    """JSON schema with every field required, so constrained decoding can't skip any.

    Field order matters too: the model writes fields in schema order, so put facts
    before verdicts.
    """
    schema = model.model_json_schema()
    schema["required"] = list(schema["properties"])
    return schema


def ollama_json(prompt: str, model: type[T], client: httpx.Client | None = None,
                temperature: float = 0.0) -> T | None:
    """Ask the local model for JSON matching `model`. Returns None on any failure; never guesses."""
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.post(
            f"{env('OLLAMA_URL', 'http://localhost:11434')}/api/chat",
            json={
                "model": env("OLLAMA_MODEL", "llama3.1:8b"),
                "messages": [{"role": "user", "content": prompt}],
                "format": llm_schema(model),
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return model.model_validate(json.loads(resp.json()["message"]["content"]))
    except (httpx.HTTPError, KeyError, json.JSONDecodeError, ValidationError) as exc:
        log.warning("LLM call for %s failed: %s", model.__name__, exc)
        return None
