#!/usr/bin/env python3
"""
Validate a generated catalog tree against the JSON schemas in schemas/, and cross-check that
every reference inside it actually resolves (details_file -> file on disk, image URL -> an
image file that exists locally under images/packs/).

This is the gate publish_catalog.py relies on before pushing anything to GitHub. Run it against
the staged output of `migrate_catalog.py generate` (migration-workspace/generated-catalog/catalog)
or against the already-published catalog/ directory.

Usage:
    python3 validate_catalog.py --catalog-dir migration-workspace/generated-catalog/catalog
    python3 validate_catalog.py --catalog-dir catalog   # validate what's already published
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

import common

log = common.setup_logging("validate_catalog")

try:
    import jsonschema
except ImportError:
    jsonschema = None


def load_schema(name: str) -> dict:
    return common.read_json(common.REPO_ROOT / "schemas" / name)


def validate_against_schema(instance, schema: dict, label: str, errors: list[str]) -> None:
    if jsonschema is None:
        errors.append(
            f"jsonschema package is not installed; skipped schema validation for {label}. "
            "Run `pip install -r requirements.txt`."
        )
        return
    validator = jsonschema.Draft7Validator(schema)
    for err in sorted(validator.iter_errors(instance), key=str):
        errors.append(f"{label}: {err.message} (path: {list(err.absolute_path)})")


def local_image_path_for_url(url: str, images_root: Path) -> Path | None:
    """Map a raw.githubusercontent.com URL (or a bare 'images/...' relative path) back to a local
    file under `images_root`, so we can check it actually exists."""
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path if parsed.scheme else url
    marker = "/images/packs/"
    if marker in path:
        rel = "images/packs/" + path.split(marker, 1)[1]
    elif path.startswith("images/packs/"):
        rel = path
    else:
        return None
    return images_root / rel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog-dir", default=str(common.GENERATED_CATALOG_DIR / "catalog"),
                         help="Directory containing manifest.json, packs/, pack-details/.")
    parser.add_argument("--images-root", default=str(common.WORKSPACE),
                         help="Root to resolve images/packs/... references against. Defaults to "
                              "migration-workspace (staged assets); pass the repo root once published.")
    args = parser.parse_args()

    catalog_dir = Path(args.catalog_dir)
    images_root = Path(args.images_root)
    errors: list[str] = []

    manifest_path = catalog_dir / "manifest.json"
    if not manifest_path.exists():
        log.error("manifest.json not found at %s", manifest_path)
        print("NOT_READY")
        return 1
    manifest = common.read_json(manifest_path)
    validate_against_schema(manifest, load_schema("manifest.schema.json"), "manifest.json", errors)

    pack_page_schema = load_schema("pack-page.schema.json")
    pack_detail_schema = load_schema("pack-detail.schema.json")

    total_packs_seen = 0
    for page_entry in manifest.get("pages", []):
        page_file = common.REPO_ROOT / page_entry["file"] if not Path(page_entry["file"]).is_absolute() else Path(page_entry["file"])
        # page_entry["file"] is relative to the published repo root (e.g. "catalog/packs/page_0.json");
        # when validating staged output, re-root it under catalog_dir's parent instead.
        candidate = catalog_dir.parent / page_entry["file"]
        page_path = candidate if candidate.exists() else page_file
        if not page_path.exists():
            errors.append(f"manifest references missing page file: {page_entry['file']}")
            continue
        page = common.read_json(page_path)
        validate_against_schema(page, pack_page_schema, page_entry["file"], errors)
        total_packs_seen += len(page.get("packs", []))

        for pack in page.get("packs", []):
            details_rel = pack["details_file"]
            details_candidate = catalog_dir.parent / details_rel
            details_path = details_candidate if details_candidate.exists() else (common.REPO_ROOT / details_rel)
            if not details_path.exists():
                errors.append(f"pack {pack['identifier']}: details_file does not resolve: {details_rel}")
                continue
            detail = common.read_json(details_path)
            validate_against_schema(detail, pack_detail_schema, details_rel, errors)

            for url_key in ("tray_image_url",):
                url = pack.get(url_key) or detail.get(url_key)
                if url:
                    img = local_image_path_for_url(url, images_root)
                    if img and not img.exists():
                        errors.append(f"pack {pack['identifier']}: {url_key} has no local file ({img})")
            for sticker in detail.get("stickers", []):
                for url_key in ("thumbnail_url", "image_url"):
                    url = sticker.get(url_key)
                    if url:
                        img = local_image_path_for_url(url, images_root)
                        if img and not img.exists():
                            errors.append(f"pack {pack['identifier']}: sticker {url_key} has no local file ({img})")

    if manifest.get("total_packs") != total_packs_seen:
        errors.append(
            f"manifest.total_packs ({manifest.get('total_packs')}) does not match packs actually "
            f"found across pages ({total_packs_seen})"
        )

    if errors:
        log.error("validation found %d problem(s):", len(errors))
        for e in errors:
            log.error(" - %s", e)
        print("NOT_READY")
        return 1

    log.info("catalog is structurally valid: %d packs across %d pages", total_packs_seen, len(manifest.get("pages", [])))
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
