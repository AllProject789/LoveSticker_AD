#!/usr/bin/env python3
"""
Romantic sticker catalog migration pipeline (Stages A-E of CLAUDE_IMPLEMENTATION_GUIDE.md).

Subcommands (each restartable, operates only on migration-workspace/ until `generate`
stages output for publish_catalog.py to push):

    fetch            Stage A - fetch raw pages 0-38 from the source API.
    parse            Stage B - normalize raw pages into a pack/sticker list.
    download         Stage C - download tray/thumbnail/sticker images.
    validate-images  Stage D - verify every downloaded image, hash it, flag problems.
    generate         Stage E - build catalog/manifest.json + page/detail JSON using
                     GitHub raw URLs (requires GITHUB_OWNER/GITHUB_REPOSITORY).
    all              Run fetch -> parse -> download -> validate-images -> generate.
    report           Re-emit the combined migration report from existing stage reports.

Nothing here pushes to GitHub - see publish_catalog.py for Stage F.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import sys
import time
from pathlib import Path

from PIL import Image

import common

log = common.setup_logging("migrate_catalog")


# --------------------------------------------------------------------------- Stage A: fetch

def fetch_page(session, page: int) -> dict:
    endpoint = common.ENDPOINT_TEMPLATE.format(page=page)
    attempt_log = []
    for host in (common.PRIMARY_HOST, common.FALLBACK_HOST):
        url = host.rstrip("/") + endpoint
        started = time.time()
        try:
            resp = session.get(url, timeout=(10, 30))
            entry = {
                "page": page,
                "requested_url": url,
                "final_url": resp.url,
                "host_used": host,
                "status": resp.status_code,
                "content_type": resp.headers.get("Content-Type"),
                "byte_count": len(resp.content or b""),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": round(time.time() - started, 3),
            }
            attempt_log.append(entry)
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError as e:
                entry["error"] = f"non-JSON response: {e}"
                continue
            if not isinstance(payload, list):
                entry["error"] = f"expected a JSON array, got {type(payload).__name__}"
                continue
            entry["success"] = True
            raw_path = common.RAW_API_DIR / f"page_{page}.json"
            common.write_json_atomic(raw_path, payload)
            return {"page": page, "success": True, "pack_count": len(payload), "attempts": attempt_log}
        except Exception as e:  # network error, timeout, connection refused, etc.
            attempt_log.append({
                "page": page, "requested_url": url, "host_used": host,
                "error": str(e), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            continue
    return {"page": page, "success": False, "pack_count": 0, "attempts": attempt_log}


def stage_fetch(pages: list[int], resume: bool, dry_run: bool, retries: int) -> dict:
    common.ensure_workspace()
    if dry_run:
        log.info("[dry-run] would fetch pages: %s", pages)
        return {"pages": pages, "dry_run": True}

    session = common.build_session(retries=retries)
    results = []
    for page in pages:
        raw_path = common.RAW_API_DIR / f"page_{page}.json"
        if resume and raw_path.exists():
            try:
                existing = common.read_json(raw_path)
                log.info("page %s already fetched (%d packs), skipping (--resume)", page, len(existing))
                results.append({"page": page, "success": True, "pack_count": len(existing), "skipped": True})
                continue
            except Exception:
                log.warning("page %s cached file is unreadable, refetching", page)
        log.info("fetching page %s ...", page)
        result = fetch_page(session, page)
        if result["success"]:
            log.info("page %s: %d packs", page, result["pack_count"])
        else:
            log.error("page %s FAILED on both hosts", page)
        results.append(result)

    common.write_json_atomic(common.REPORTS_DIR / "fetch_report.json", {"pages": results})
    failed = [r["page"] for r in results if not r["success"]]
    if failed:
        log.warning("fetch stage finished with failures on pages: %s", failed)
    return {"pages": results}


# --------------------------------------------------------------------------- Stage B: parse

def normalize_pack(raw: dict, page: int, base_host: str) -> dict | None:
    if "identifier" not in raw or raw.get("identifier") in (None, ""):
        log.warning("page %s: dropping pack with missing identifier: %r", page, raw)
        return None
    if not raw.get("name"):
        log.warning("page %s: dropping pack %r with missing name", page, raw.get("identifier"))
        return None

    pack = {"identifier": raw["identifier"], "name": raw["name"], "source_page": page}
    for key, default in common.PACK_DEFAULTS.items():
        pack[key] = raw.get(key, default)
        if pack[key] is None:
            pack[key] = default

    pack["tray_image_url"] = common.resolve_image_url(pack.pop("tray_image_file", "") or raw.get("tray_image_file", ""), base_host)

    stickers = []
    for s in pack.get("stickers") or []:
        sticker = {}
        for key, default in common.STICKER_DEFAULTS.items():
            sticker[key] = s.get(key, default)
            if sticker[key] is None:
                sticker[key] = default
        sticker["thumbnail_url"] = common.resolve_image_url(sticker.pop("image_file_thum", ""), base_host)
        sticker["image_url"] = common.resolve_image_url(sticker.pop("image_file", ""), base_host)
        sticker["emojis"] = [common.decode_emoji(e) for e in sticker["emojis"]]
        stickers.append(sticker)
    pack["stickers"] = stickers
    return pack


def stage_parse(dry_run: bool) -> dict:
    common.ensure_workspace()
    fetch_report_path = common.REPORTS_DIR / "fetch_report.json"
    if not fetch_report_path.exists():
        raise RuntimeError("No fetch_report.json found. Run `fetch` before `parse`.")
    fetch_report = common.read_json(fetch_report_path)

    host_by_page: dict[int, str] = {}
    for entry in fetch_report["pages"]:
        if entry.get("success"):
            host = entry.get("host_used")
            if not host:
                for a in entry.get("attempts", []):
                    if a.get("success") or a.get("status") == 200:
                        host = a.get("host_used")
            host_by_page[entry["page"]] = host or common.PRIMARY_HOST

    if dry_run:
        log.info("[dry-run] would parse pages: %s", sorted(host_by_page))
        return {"dry_run": True}

    all_packs: list[dict] = []
    seen_ids: dict = {}
    duplicates: list[dict] = []

    for page, host in sorted(host_by_page.items()):
        raw_path = common.RAW_API_DIR / f"page_{page}.json"
        if not raw_path.exists():
            continue
        raw_packs = common.read_json(raw_path)
        for raw in raw_packs:
            pack = normalize_pack(raw, page, host)
            if pack is None:
                continue
            pid = pack["identifier"]
            if pid in seen_ids:
                duplicates.append({"identifier": pid, "pages": [seen_ids[pid], page]})
            else:
                seen_ids[pid] = page
            all_packs.append(pack)

    common.write_json_atomic(common.GENERATED_CATALOG_DIR / "normalized_packs.json", all_packs)
    common.write_json_atomic(common.REPORTS_DIR / "parse_report.json", {
        "total_packs_parsed": len(all_packs),
        "unique_pack_ids": len(seen_ids),
        "duplicate_pack_ids": duplicates,
        "pages_parsed": sorted(host_by_page),
    })
    log.info("parsed %d packs (%d unique ids, %d duplicates)", len(all_packs), len(seen_ids), len(duplicates))
    return {"total_packs": len(all_packs), "duplicates": duplicates}


# --------------------------------------------------------------------------- Stage C: download

def _existing_file(directory: Path, stem: str) -> Path | None:
    for ext in (".webp", ".png", ".jpg", ".jpeg", ".gif"):
        candidate = directory / f"{stem}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def download_one(session, url: str, targets: list[tuple[Path, str]], retries: int) -> list[dict]:
    """Download `url` once, detect its REAL format (never trust the source's claimed extension -
    some 'tray_image_file' URLs are actually PNG), then fan the bytes out to every destination
    that needs them (the source API reuses one file for both image_file_thum and image_file)."""
    canonical_dir, canonical_stem = targets[0]
    if not url:
        return [{"url": url, "local_path": str(d / s), "status": "no_url", "success": False,
                  "error": "empty source url"} for d, s in targets]

    canonical = _existing_file(canonical_dir, canonical_stem)
    if canonical is None:
        canonical_dir.mkdir(parents=True, exist_ok=True)
        tmp = canonical_dir / f"{canonical_stem}.part"
        last_error = None
        downloaded = False
        for attempt in range(1, retries + 1):
            try:
                with session.get(url, stream=True, timeout=(10, 60)) as resp:
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        time.sleep(min(2 ** attempt, 15))
                        continue
                    with tmp.open("wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 64):
                            if chunk:
                                f.write(chunk)
                    if tmp.stat().st_size == 0:
                        last_error = "downloaded zero bytes"
                        tmp.unlink(missing_ok=True)
                        time.sleep(min(2 ** attempt, 15))
                        continue
                    ext = common.detect_image_extension(tmp)
                    canonical = canonical_dir / f"{canonical_stem}{ext}"
                    tmp.replace(canonical)
                    downloaded = True
                    break
            except Exception as e:
                last_error = str(e)
                time.sleep(min(2 ** attempt, 15))
        if not downloaded:
            tmp.unlink(missing_ok=True)
            return [{"url": url, "local_path": str(d / s), "status": "failed", "success": False,
                      "error": last_error} for d, s in targets]
        status = "downloaded"
    else:
        status = "skipped_existing"

    results = [{"url": url, "local_path": str(canonical), "status": status, "success": True,
                "bytes": canonical.stat().st_size}]
    ext = canonical.suffix
    for extra_dir, extra_stem in targets[1:]:
        existing = _existing_file(extra_dir, extra_stem)
        if existing is not None:
            results.append({"url": url, "local_path": str(existing), "status": "skipped_existing", "success": True,
                             "bytes": existing.stat().st_size})
            continue
        extra_dir.mkdir(parents=True, exist_ok=True)
        extra = extra_dir / f"{extra_stem}{ext}"
        try:
            try:
                os.link(canonical, extra)  # hardlink when possible: no extra disk usage
            except OSError:
                shutil.copyfile(canonical, extra)
            results.append({"url": url, "local_path": str(extra), "status": "linked_from_shared_source",
                             "success": True, "bytes": extra.stat().st_size})
        except Exception as e:
            results.append({"url": url, "local_path": str(extra), "status": "failed", "success": False, "error": str(e)})
    return results


def stage_download(dry_run: bool, concurrency: int, retries: int) -> dict:
    common.ensure_workspace()
    packs_path = common.GENERATED_CATALOG_DIR / "normalized_packs.json"
    if not packs_path.exists():
        raise RuntimeError("No normalized_packs.json found. Run `parse` before `download`.")
    packs = common.read_json(packs_path)

    url_to_targets: dict[str, list[tuple[Path, str]]] = {}

    def plan(url: str, directory: Path, stem: str):
        if not url:
            return
        url_to_targets.setdefault(url, []).append((directory, stem))

    for pack in packs:
        pid = str(pack["identifier"])
        pack_dir = common.WORKSPACE_IMAGES_DIR / pid
        plan(pack["tray_image_url"], pack_dir, "tray")
        for i, sticker in enumerate(pack["stickers"], start=1):
            idx = f"{i:03d}"
            plan(sticker["thumbnail_url"], pack_dir / "thumbnails", idx)
            plan(sticker["image_url"], pack_dir / "stickers", idx)

    jobs = list(url_to_targets.items())
    total_dests = sum(len(d) for _, d in jobs)

    if dry_run:
        log.info("[dry-run] would download %d unique source URLs (%d destination files) across %d packs",
                  len(jobs), total_dests, len(packs))
        return {"dry_run": True, "planned_downloads": total_dests}

    session = common.build_session(retries=1)  # per-attempt retry handled in download_one
    results: list[dict] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(download_one, session, url, dests, retries): url for url, dests in jobs}
        for fut in concurrent.futures.as_completed(futures):
            entries = fut.result()
            results.extend(entries)
            done += 1
            if done % 100 == 0 or done == len(jobs):
                log.info("processed %d/%d source URLs (%d/%d files)", done, len(jobs), len(results), total_dests)
            for r in entries:
                if not r["success"]:
                    log.error("FAILED %s -> %s (%s)", r["url"], r["local_path"], r.get("error"))

    common.write_json_atomic(common.REPORTS_DIR / "download_report.json", {"assets": results})
    succeeded = sum(1 for r in results if r["success"])
    log.info("download stage: %d/%d files ok", succeeded, len(results))
    return {"total": len(results), "succeeded": succeeded, "failed": len(results) - succeeded}


# --------------------------------------------------------------------------- Stage D: validate images

def validate_image(path: Path) -> dict:
    result = {"path": str(path), "exists": path.exists()}
    if not result["exists"]:
        result["valid"] = False
        result["error"] = "missing"
        return result
    size = path.stat().st_size
    result["bytes"] = size
    if size == 0:
        result["valid"] = False
        result["error"] = "empty file"
        return result
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:  # verify() invalidates the handle; reopen to inspect
            result["format"] = im.format
            result["width"], result["height"] = im.size
            result["has_alpha"] = im.mode in ("RGBA", "LA") or "transparency" in im.info
            frames = getattr(im, "n_frames", 1)
            result["animated"] = frames > 1
            result["frame_count"] = frames
        result["sha256"] = common.sha256_of_file(path)
        result["valid"] = True
    except Exception as e:
        result["valid"] = False
        result["error"] = f"decode failed: {e}"
    return result


def stage_validate_images(dry_run: bool) -> dict:
    common.ensure_workspace()
    download_report_path = common.REPORTS_DIR / "download_report.json"
    if not download_report_path.exists():
        raise RuntimeError("No download_report.json found. Run `download` before `validate-images`.")
    download_report = common.read_json(download_report_path)
    paths = [Path(a["local_path"]) for a in download_report["assets"] if a["success"]]

    if dry_run:
        log.info("[dry-run] would validate %d images", len(paths))
        return {"dry_run": True, "planned_validations": len(paths)}

    results = []
    hashes: dict[str, list[str]] = {}
    for i, p in enumerate(paths, start=1):
        r = validate_image(p)
        results.append(r)
        if r.get("sha256"):
            hashes.setdefault(r["sha256"], []).append(r["path"])
        if i % 200 == 0 or i == len(paths):
            log.info("validated %d/%d images", i, len(paths))

    duplicates = {h: ps for h, ps in hashes.items() if len(ps) > 1}
    non_webp = [r for r in results if r.get("valid") and r.get("format") != "WEBP"]
    invalid = [r for r in results if not r.get("valid")]

    common.write_json_atomic(common.REPORTS_DIR / "validation_report.json", {
        "images": results,
        "duplicate_hashes": duplicates,
        "non_webp_count": len(non_webp),
        "invalid_count": len(invalid),
    })
    log.info("validation: %d ok, %d invalid, %d non-webp, %d duplicate groups",
              len(results) - len(invalid), len(invalid), len(non_webp), len(duplicates))
    return {"total": len(results), "invalid": len(invalid), "non_webp": len(non_webp), "duplicates": len(duplicates)}


# --------------------------------------------------------------------------- Stage E: generate

def stage_generate(dry_run: bool) -> dict:
    common.ensure_workspace()
    gh = common.load_github_config()
    packs_path = common.GENERATED_CATALOG_DIR / "normalized_packs.json"
    validation_path = common.REPORTS_DIR / "validation_report.json"
    if not packs_path.exists():
        raise RuntimeError("No normalized_packs.json found. Run `parse` before `generate`.")
    if not validation_path.exists():
        raise RuntimeError("No validation_report.json found. Run `validate-images` before `generate`.")
    if dry_run:
        log.info("[dry-run] would generate manifest/page/detail JSON using repo=%s/%s branch=%s",
                  gh.owner, gh.repository, gh.branch)
        return {"dry_run": True}
    if not gh.configured:
        raise RuntimeError(
            "GITHUB_OWNER and GITHUB_REPOSITORY must be set (env vars or .env) before generating "
            "GitHub raw URLs. See .env.example."
        )

    packs = common.read_json(packs_path)
    validation = common.read_json(validation_path)
    valid_by_path = {v["path"]: v for v in validation["images"] if v.get("valid")}

    def find_valid_local(directory: Path, stem: str) -> Path | None:
        """Resolve a validated local file by stem, whatever its real extension turned out to be -
        download stage never renames a PNG/JPEG to .webp, so the extension isn't known in advance."""
        found = _existing_file(directory, stem)
        if found is not None and str(found) in valid_by_path:
            return found
        return None

    staging = common.GENERATED_CATALOG_DIR / "catalog"
    staging_packs = staging / "packs"
    staging_details = staging / "pack-details"
    for d in (staging_packs, staging_details):
        d.mkdir(parents=True, exist_ok=True)

    pages: dict[int, list[dict]] = {}
    skipped_packs = []

    for pack in packs:
        pid = str(pack["identifier"])
        pack_dir = common.WORKSPACE_IMAGES_DIR / pid
        tray_local = find_valid_local(pack_dir, "tray")
        if pack["tray_image_url"] and tray_local is None:
            skipped_packs.append({"identifier": pid, "reason": "tray image missing or invalid"})
            continue

        stickers_out = []
        pack_ok = True
        for i, sticker in enumerate(pack["stickers"], start=1):
            idx = f"{i:03d}"
            thumb_local = find_valid_local(pack_dir / "thumbnails", idx)
            full_local = find_valid_local(pack_dir / "stickers", idx)
            if sticker["thumbnail_url"] and thumb_local is None:
                pack_ok = False
                break
            if sticker["image_url"] and full_local is None:
                pack_ok = False
                break
            stickers_out.append({
                "thumbnail_url": gh.raw_url(f"images/packs/{pid}/thumbnails/{thumb_local.name}") if sticker["thumbnail_url"] else "",
                "image_url": gh.raw_url(f"images/packs/{pid}/stickers/{full_local.name}") if sticker["image_url"] else "",
                "emojis": sticker["emojis"],
            })
        if not pack_ok:
            skipped_packs.append({"identifier": pid, "reason": "one or more sticker assets missing or invalid"})
            continue

        tray_url = gh.raw_url(f"images/packs/{pid}/{tray_local.name}") if pack["tray_image_url"] else ""
        detail = {
            "identifier": pack["identifier"],
            "name": pack["name"],
            "publisher": pack["publisher"],
            "tray_image_url": tray_url,
            "premium": pack["premium"],
            "animated": pack["animated"],
            "stickers": stickers_out,
        }
        details_file = f"catalog/pack-details/{pid}.json"
        common.write_json_atomic(staging_details / f"{pid}.json", detail)

        summary = {
            "identifier": pack["identifier"],
            "name": pack["name"],
            "publisher": pack["publisher"],
            "tray_image_url": tray_url,
            "premium": pack["premium"],
            "animated": pack["animated"],
            "downloads": pack["downloads"],
            "size": pack["size"],
            "created": pack["created"],
            "sticker_count": len(stickers_out),
            "details_file": details_file,
        }
        pages.setdefault(pack["source_page"], []).append(summary)

    for page, pack_list in pages.items():
        common.write_json_atomic(staging_packs / f"page_{page}.json", {"page": page, "packs": pack_list})

    manifest = {
        "schema_version": 1,
        "catalog_version": time.strftime("%Y.%m.%d-%H%M%S", time.gmtime()),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_packs": sum(len(v) for v in pages.values()),
        "total_pages": len(pages),
        "pages": [{"page": p, "file": f"catalog/packs/page_{p}.json"} for p in sorted(pages)],
        "repository": {"owner": gh.owner, "name": gh.repository, "branch": gh.branch},
    }
    common.write_json_atomic(staging / "manifest.json", manifest)
    common.write_json_atomic(common.REPORTS_DIR / "generate_report.json", {
        "total_packs_included": manifest["total_packs"],
        "total_pages": manifest["total_pages"],
        "skipped_packs": skipped_packs,
    })
    log.info("generated catalog: %d packs across %d pages (%d packs skipped - see generate_report.json)",
              manifest["total_packs"], manifest["total_pages"], len(skipped_packs))
    log.info("staged output: %s", staging)
    return {"total_packs": manifest["total_packs"], "skipped": len(skipped_packs)}


# --------------------------------------------------------------------------- report

def write_combined_report() -> dict:
    common.ensure_workspace()
    report: dict = {"stages": {}}
    for name in ("fetch_report", "parse_report", "download_report", "validation_report", "generate_report"):
        p = common.REPORTS_DIR / f"{name}.json"
        if p.exists():
            report["stages"][name] = common.read_json(p)

    fetch = report["stages"].get("fetch_report", {}).get("pages", [])
    failed_pages = [r["page"] for r in fetch if not r.get("success")]
    validation = report["stages"].get("validation_report", {})
    download = report["stages"].get("download_report", {})
    ready = (
        bool(fetch) and not failed_pages
        and download and download.get("assets") and all(a["success"] for a in download["assets"])
        and validation and validation.get("invalid_count", 1) == 0
        and "generate_report" in report["stages"]
    )
    report["final_status"] = "READY" if ready else "NOT_READY"
    report["failed_pages"] = failed_pages
    common.write_json_atomic(common.REPORTS_DIR / "migration-report.json", report)

    md = [f"# Migration Report\n", f"**Final status:** `{report['final_status']}`\n"]
    if failed_pages:
        md.append(f"**Failed pages:** {failed_pages}\n")
    parse = report["stages"].get("parse_report", {})
    if parse:
        md.append(f"- Packs parsed: {parse.get('total_packs_parsed')} "
                   f"(unique: {parse.get('unique_pack_ids')}, duplicates: {len(parse.get('duplicate_pack_ids', []))})\n")
    if download:
        assets = download.get("assets", [])
        ok = sum(1 for a in assets if a["success"])
        md.append(f"- Assets downloaded: {ok}/{len(assets)}\n")
    if validation:
        md.append(f"- Images validated: {len(validation.get('images', []))}, "
                   f"invalid: {validation.get('invalid_count')}, non-webp: {validation.get('non_webp_count')}, "
                   f"duplicate hash groups: {len(validation.get('duplicate_hashes', {}))}\n")
    gen = report["stages"].get("generate_report", {})
    if gen:
        md.append(f"- Packs included in generated catalog: {gen.get('total_packs_included')} "
                   f"(skipped: {len(gen.get('skipped_packs', []))})\n")
    (common.REPORTS_DIR / "migration-report.md").write_text("\n".join(md), encoding="utf-8")
    log.info("final status: %s", report["final_status"])
    return report


# --------------------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=[
        "fetch", "parse", "download", "validate-images", "generate", "all", "report",
    ])
    parser.add_argument("--pages", default="0-38", help="Page range for `fetch`, e.g. '0-38' or '0,1,5'.")
    parser.add_argument("--dry-run", action="store_true", help="Plan the stage without touching network or disk state.")
    parser.add_argument("--resume", action="store_true", help="Skip pages already fetched in a previous run.")
    parser.add_argument("--concurrency", type=int, default=8, help="Parallel downloads for the `download` stage.")
    parser.add_argument("--retries", type=int, default=4, help="Retry attempts per request.")
    parser.add_argument("--force-unlock", action="store_true", help="Ignore a stale migration lock.")
    args = parser.parse_args()

    common.ensure_workspace()
    lock = common.MigrationLock()
    lock.acquire(force=args.force_unlock)
    try:
        if args.stage in ("fetch", "all"):
            stage_fetch(common.parse_page_range(args.pages), args.resume, args.dry_run, args.retries)
        if args.stage in ("parse", "all"):
            stage_parse(args.dry_run)
        if args.stage in ("download", "all"):
            stage_download(args.dry_run, args.concurrency, args.retries)
        if args.stage in ("validate-images", "all"):
            stage_validate_images(args.dry_run)
        if args.stage in ("generate", "all"):
            stage_generate(args.dry_run)
        if args.stage in ("report", "all") and not args.dry_run:
            write_combined_report()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
