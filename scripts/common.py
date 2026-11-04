"""Shared config, HTTP session, and workspace helpers for the catalog migration pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "migration-workspace"
RAW_API_DIR = WORKSPACE / "raw-api"
WORKSPACE_IMAGES_DIR = WORKSPACE / "images" / "packs"
GENERATED_CATALOG_DIR = WORKSPACE / "generated-catalog"
REPORTS_DIR = WORKSPACE / "reports"
LOCK_FILE = WORKSPACE / ".migration.lock"

PRIMARY_HOST = "https://lovestickers.prdcreations.com"
FALLBACK_HOST = "https://romanticws.bgremover.us"
ENDPOINT_TEMPLATE = "/api/pack/all/{page}/created/1098726880577/ab3ce0af-b419-4200-846d-cc9d71965d3f/"
USER_AGENT = "romantic-sticker-catalog-migrator/1.0 (+catalog-migration-pipeline)"

# File fields observed in the decompiled Android serializer, with their defaults.
PACK_DEFAULTS: dict[str, Any] = {
    "publisher": "",
    "tray_image_file": "",
    "premium": "false",
    "animated": "false",
    "downloads": "",
    "size": "",
    "created": "",
    "stickers": [],
}
STICKER_DEFAULTS: dict[str, Any] = {
    "image_file_thum": "",
    "image_file": "",
    "emojis": [],
}

_SECRET_ENV_KEYS = {"GITHUB_TOKEN", "GH_TOKEN"}


def setup_logging(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def ensure_workspace() -> None:
    for d in (RAW_API_DIR, WORKSPACE_IMAGES_DIR, GENERATED_CATALOG_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader. Never overwrites a variable already set in the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class GithubConfig:
    owner: str | None
    repository: str | None
    branch: str

    @property
    def configured(self) -> bool:
        return bool(self.owner and self.repository)

    def raw_url(self, path_in_repo: str) -> str:
        if not self.configured:
            raise RuntimeError(
                "GITHUB_OWNER / GITHUB_REPOSITORY are not set. Set them (env vars or .env) "
                "before generating URLs that point at the hosted repository."
            )
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repository}/{self.branch}/{path_in_repo}"


def load_github_config() -> GithubConfig:
    load_dotenv()
    return GithubConfig(
        owner=os.environ.get("GITHUB_OWNER"),
        repository=os.environ.get("GITHUB_REPOSITORY"),
        branch=os.environ.get("GITHUB_BRANCH", "main"),
    )


def decode_emoji(value: str) -> str:
    """The source API serializes each emoji as base64-encoded UTF-8 (observed: '8J+YgA==' -> '😀').
    Fall back to the raw value if it isn't valid base64/UTF-8, so we never fabricate data."""
    if not value:
        return value
    try:
        import base64
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        if decoded:
            return decoded
    except Exception:
        pass
    return value


def redact(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…redacted…" + value[-4:]


def parse_page_range(spec: str, max_page: int = 38) -> list[int]:
    """Parse '0-38' or '0,1,5' or '3' into a sorted list of unique page numbers."""
    pages: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_s, end_s = chunk.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start
            pages.update(range(start, end + 1))
        else:
            pages.add(int(chunk))
    return sorted(p for p in pages if p >= 0)


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_session(retries: int = 4, backoff: float = 1.0) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def resolve_image_url(path_or_url: str, base_host: str) -> str:
    """The app resolves relative image paths against the host that served the pack.
    Absolute http(s) URLs are preserved untouched."""
    if not path_or_url:
        return ""
    if re.match(r"^https?://", path_or_url, re.IGNORECASE):
        return path_or_url
    return base_host.rstrip("/") + "/" + path_or_url.lstrip("/")


def safe_extension_for(content_type: str | None, fallback: str = ".webp") -> str:
    mapping = {
        "image/webp": ".webp",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
    }
    if content_type:
        content_type = content_type.split(";")[0].strip().lower()
        if content_type in mapping:
            return mapping[content_type]
    return fallback


_FORMAT_EXTENSION = {"WEBP": ".webp", "PNG": ".png", "JPEG": ".jpg", "GIF": ".gif"}


def detect_image_extension(path: Path, fallback: str = ".webp") -> str:
    """Inspect actual file bytes (never trust a guessed extension) and return the truthful
    extension for the decoded format. The guide is explicit: never rename a PNG/JPEG to .webp."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return _FORMAT_EXTENSION.get(im.format, fallback)
    except Exception:
        return fallback


class MigrationLock:
    """Simple PID-based lock file so two migration runs can't clobber the same workspace."""

    def __init__(self, path: Path = LOCK_FILE):
        self.path = path

    def acquire(self, force: bool = False) -> None:
        if self.path.exists() and not force:
            info = self.path.read_text().strip()
            raise RuntimeError(
                f"Another migration run appears to be in progress (lock held by: {info}). "
                "If that run crashed, re-run with --force-unlock after confirming no other "
                "process is active."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")

    def release(self) -> None:
        if self.path.exists():
            self.path.unlink()


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
