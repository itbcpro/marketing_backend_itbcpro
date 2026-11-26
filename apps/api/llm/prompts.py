"""Versioned prompt files: ``prompts/<domain>/<name>/vN.md`` with TOML front matter.

Prompts never live in code. Front matter declares the purpose, model tier and the variables a
caller must supply; the body uses ``${variable}`` placeholders (no code execution).
"""

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from string import Template

from django.conf import settings

from core.errors import DomainError, ErrorCode

FRONT_MATTER = re.compile(r"\A\+\+\+\n(?P<meta>.*?)\n\+\+\+\n(?P<body>.*)\Z", re.S)
VERSION_FILE = re.compile(r"^v(\d+)\.md$")
MAX_RENDERED_CHARS = 200_000


class PromptError(DomainError):
    code = ErrorCode.VALIDATION_FAILED
    http_status = 500


@dataclass(frozen=True, slots=True)
class Prompt:
    key: str
    version: int
    purpose: str
    tier: str
    variables: tuple[str, ...]
    body: str
    output_schema: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def render(self, **values: object) -> str:
        missing = set(self.variables) - set(values)
        unknown = set(values) - set(self.variables)
        if missing or unknown:
            raise PromptError(
                f"{self.key}@v{self.version}: missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        text = Template(self.body).substitute({k: str(v) for k, v in values.items()})
        if len(text) > MAX_RENDERED_CHARS:
            raise PromptError(f"{self.key}: rendered prompt is too large")
        return text


def _parse(key: str, version: int, raw: str) -> Prompt:
    match = FRONT_MATTER.match(raw)
    if not match:
        raise PromptError(f"{key}@v{version}: missing +++ front matter")
    meta = tomllib.loads(match["meta"])
    for required in ("purpose", "tier"):
        if required not in meta:
            raise PromptError(f"{key}@v{version}: front matter needs '{required}'")
    body = match["body"].strip() + "\n"
    declared = set(meta.get("variables", []))
    used = set(re.findall(r"\$\{(\w+)\}", body))
    if used != declared:
        raise PromptError(
            f"{key}@v{version}: variables {sorted(declared)} "
            f"do not match placeholders {sorted(used)}"
        )
    return Prompt(
        key=key,
        version=version,
        purpose=str(meta["purpose"]),
        tier=str(meta["tier"]),
        variables=tuple(meta.get("variables", [])),
        body=body,
        output_schema=str(meta.get("output_schema", "")),
        extra={
            k: v
            for k, v in meta.items()
            if k not in {"purpose", "tier", "variables", "output_schema"}
        },
    )


def _root() -> Path:
    return Path(settings.PROMPTS_DIR)


def available_versions(key: str) -> list[int]:
    folder = _root() / key
    if not folder.is_dir():
        return []
    return sorted(int(m[1]) for p in folder.iterdir() if (m := VERSION_FILE.match(p.name)))


@lru_cache(maxsize=256)
def _load(root: str, key: str, version: int) -> Prompt:
    path = Path(root) / key / f"v{version}.md"
    return _parse(key, version, path.read_text(encoding="utf-8"))


def clear_cache() -> None:
    _load.cache_clear()


def get_prompt(key: str, version: int | None = None) -> Prompt:
    """Load a prompt; without ``version`` the newest file wins."""
    versions = available_versions(key)
    if not versions:
        raise PromptError(f"unknown prompt: {key}")
    chosen = version if version is not None else versions[-1]
    if chosen not in versions:
        raise PromptError(f"{key}: version {chosen} does not exist")
    return _load(str(_root()), key, chosen)


def all_prompt_keys() -> list[str]:
    root = _root()
    keys: set[str] = set()
    for path in root.rglob("v*.md"):
        if VERSION_FILE.match(path.name):
            keys.add(path.parent.relative_to(root).as_posix())
    return sorted(keys)
