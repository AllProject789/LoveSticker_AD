#!/usr/bin/env python3
"""
Stage F - publish the staged, validated catalog to GitHub.

This script never force-pushes, never stages unrelated files, and never pushes anything until
`validate_catalog.py` has reported READY for the staged output. By default it only prints a plan
(file counts, total bytes, largest file, estimated repo size) - pass --yes to actually copy the
staged files into the repo tree, commit, and push.

Requires:
  - GITHUB_OWNER, GITHUB_REPOSITORY, GITHUB_BRANCH set (env vars or .env)
  - a `git remote` already configured for this repo pointing at that owner/repository
  - a working `git` credential setup (credential manager / SSH key) - this script never
    handles tokens or passwords directly

Usage:
    python3 publish_catalog.py                 # dry-run: show the plan only
    python3 publish_catalog.py --yes            # copy + commit + push after the plan is shown
    python3 publish_catalog.py --yes --sample 25  # verify 25 random image URLs after push
    python3 publish_catalog.py --yes --full-verify  # verify every image URL after push (slow)
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
from pathlib import Path

import requests

import common

log = common.setup_logging("publish_catalog")

GITHUB_SINGLE_FILE_HARD_LIMIT = 100 * 1024 * 1024   # GitHub rejects files above this.
GITHUB_SINGLE_FILE_WARN_LIMIT = 25 * 1024 * 1024    # worth calling out explicitly.


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def confirm_git_setup(repo_dir: Path, gh: common.GithubConfig) -> None:
    if not (repo_dir / ".git").exists():
        raise RuntimeError(
            f"{repo_dir} is not a git repository yet. Run `git init`, add the GitHub remote "
            f"(git remote add origin git@github.com:{gh.owner}/{gh.repository}.git or the https "
            "form), and make an initial commit before publishing."
        )
    remotes = run_git(["remote", "-v"], repo_dir)
    if gh.configured and gh.repository not in remotes:
        log.warning(
            "configured remote(s) do not obviously reference GITHUB_REPOSITORY=%s - double check "
            "`git remote -v` before pushing:\n%s", gh.repository, remotes
        )
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    if branch != gh.branch:
        log.warning("current branch is '%s' but GITHUB_BRANCH is '%s'", branch, gh.branch)


def collect_plan(staging_catalog: Path, staging_images: Path) -> dict:
    files: list[Path] = []
    if staging_catalog.exists():
        files.extend(p for p in staging_catalog.rglob("*") if p.is_file())
    if staging_images.exists():
        files.extend(p for p in staging_images.rglob("*") if p.is_file())

    sizes = [(f, f.stat().st_size) for f in files]
    total_bytes = sum(s for _, s in sizes)
    largest = max(sizes, key=lambda t: t[1]) if sizes else (None, 0)
    oversized = [(f, s) for f, s in sizes if s > GITHUB_SINGLE_FILE_HARD_LIMIT]
    warn_sized = [(f, s) for f, s in sizes if GITHUB_SINGLE_FILE_WARN_LIMIT < s <= GITHUB_SINGLE_FILE_HARD_LIMIT]
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "largest_file": str(largest[0]) if largest[0] else None,
        "largest_bytes": largest[1],
        "oversized": [(str(f), s) for f, s in oversized],
        "warn_sized": [(str(f), s) for f, s in warn_sized],
        "files": files,
    }


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def copy_staged_into_repo(repo_dir: Path, staging_catalog: Path, staging_images: Path) -> None:
    if staging_catalog.exists():
        shutil.copytree(staging_catalog, repo_dir / "catalog", dirs_exist_ok=True)
    if staging_images.exists():
        shutil.copytree(staging_images, repo_dir / "images" / "packs", dirs_exist_ok=True)


def verify_urls(gh: common.GithubConfig, manifest_path: Path, sample: int, full: bool) -> dict:
    manifest = common.read_json(manifest_path)
    urls = [gh.raw_url("catalog/manifest.json")]
    for page_entry in manifest.get("pages", []):
        page_path = manifest_path.parent / Path(page_entry["file"]).relative_to("catalog")
        if page_path.exists():
            page = common.read_json(page_path)
            for pack in page.get("packs", []):
                if pack.get("tray_image_url"):
                    urls.append(pack["tray_image_url"])

    checked = urls if full else random.sample(urls, min(sample, len(urls)))
    results = []
    for url in checked:
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
            results.append({"url": url, "status": resp.status_code, "ok": resp.status_code == 200})
        except Exception as e:
            results.append({"url": url, "status": None, "ok": False, "error": str(e)})
    return {"checked": len(checked), "total_candidates": len(urls), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="Actually copy, commit, and push (otherwise dry-run only).")
    parser.add_argument("--message", default="Add sticker catalog and image assets", help="Commit message.")
    parser.add_argument("--sample", type=int, default=20, help="Number of raw URLs to verify after push.")
    parser.add_argument("--full-verify", action="store_true", help="Verify every raw URL after push instead of a sample.")
    args = parser.parse_args()

    gh = common.load_github_config()
    staging_catalog = common.GENERATED_CATALOG_DIR / "catalog"
    staging_images = common.WORKSPACE_IMAGES_DIR

    validation = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "validate_catalog.py"),
         "--catalog-dir", str(staging_catalog)],
        capture_output=True, text=True,
    )
    print(validation.stdout)
    if validation.returncode != 0:
        log.error("staged catalog is NOT_READY - fix validation errors above before publishing.")
        return 1

    plan = collect_plan(staging_catalog, staging_images)
    log.info("publish plan:")
    log.info("  files to add:      %d", plan["file_count"])
    log.info("  total size:        %s", human_bytes(plan["total_bytes"]))
    log.info("  largest file:      %s (%s)", plan["largest_file"], human_bytes(plan["largest_bytes"]))
    if plan["oversized"]:
        log.error("  %d file(s) exceed GitHub's hard per-file limit (100MB) and will be rejected:", len(plan["oversized"]))
        for f, s in plan["oversized"]:
            log.error("    - %s (%s)", f, human_bytes(s))
        return 1
    if plan["warn_sized"]:
        log.warning("  %d file(s) are larger than 25MB - confirm these are really meant to be committed as-is:", len(plan["warn_sized"]))
        for f, s in plan["warn_sized"]:
            log.warning("    - %s (%s)", f, human_bytes(s))
    if plan["total_bytes"] > 1 * 1024 * 1024 * 1024:
        log.warning(
            "  this push adds %s to the repository. A multi-GB git repository is unusual - "
            "Android clients must fetch individual raw URLs and must NEVER clone this repo.",
            human_bytes(plan["total_bytes"]),
        )

    if not gh.configured:
        log.error("GITHUB_OWNER / GITHUB_REPOSITORY are not set - nothing to publish against yet. "
                   "Set them in .env (see .env.example) once the repository exists.")
        return 1

    if not args.yes:
        log.info("dry-run only (pass --yes to copy these files into the repo, commit, and push)")
        return 0

    confirm_git_setup(common.REPO_ROOT, gh)
    copy_staged_into_repo(common.REPO_ROOT, staging_catalog, staging_images)

    run_git(["add", "catalog", "images", "schemas", "README.md"], common.REPO_ROOT)
    status = run_git(["status", "--porcelain"], common.REPO_ROOT)
    if not status:
        log.info("nothing to commit - repo already matches the staged catalog.")
        return 0

    run_git(["commit", "-m", args.message], common.REPO_ROOT)
    log.info("pushing to %s/%s (branch %s) ...", gh.owner, gh.repository, gh.branch)
    run_git(["push", "origin", gh.branch], common.REPO_ROOT)  # never --force

    manifest_path = common.REPO_ROOT / "catalog" / "manifest.json"
    verification = verify_urls(gh, manifest_path, args.sample, args.full_verify)
    ok = sum(1 for r in verification["results"] if r["ok"])
    log.info("post-push verification: %d/%d checked URLs returned 200", ok, verification["checked"])
    common.write_json_atomic(common.REPORTS_DIR / "publish_verification.json", verification)
    if ok != verification["checked"]:
        log.error("some raw URLs did not verify - do not treat this publish as fully successful. See publish_verification.json.")
        return 1

    log.info("publish complete and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
