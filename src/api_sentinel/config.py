"""Configuration loading. Environment values are resolved only inside workers."""

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from api_sentinel.models import Suite

VARIABLE = re.compile(r"\$\{([^}]+)\}")


def load_document(path: str | Path) -> dict:
    source = Path(path)
    if source.stat().st_size > 5_000_000:
        raise ValueError("configuration exceeds the 5 MB limit")
    text = source.read_text(encoding="utf-8")
    value = json.loads(text) if source.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value


def load_suite(path: str | Path) -> Suite:
    return Suite.model_validate(load_document(path))


def validate_base_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("base URL must be HTTP(S), without credentials, query, or fragment")
    return value.rstrip("/")


def render(value: Any, variables: dict[str, Any]) -> Any:
    def resolve(name: str) -> Any:
        if name.startswith("env:"):
            key = name[4:]
            if key not in os.environ:
                raise ValueError(f"missing environment variable: {key}")
            return os.environ[key]
        if name not in variables:
            raise ValueError(f"unknown template variable: {name}")
        return variables[name]

    if isinstance(value, str):
        full = VARIABLE.fullmatch(value)
        if full:
            return resolve(full.group(1))
        return VARIABLE.sub(lambda match: str(resolve(match.group(1))), value)
    if isinstance(value, list):
        return [render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render(item, variables) for key, item in value.items()}
    return value
