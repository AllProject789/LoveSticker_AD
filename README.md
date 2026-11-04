# romantic-sticker-catalog

Migration pipeline that fetches the source romantic-sticker catalog, downloads its pack/sticker
images, validates everything, and publishes a versioned catalog (JSON + images) to this GitHub
repository for the Android app to consume via `raw.githubusercontent.com` URLs.

Spec: see `CLAUDE_IMPLEMENTATION_GUIDE.md` (kept alongside this repo, not committed here) for the
full stage-by-stage requirements this pipeline implements.

## Layout

```text
catalog/              Published catalog JSON (manifest, page summaries, pack details).
images/packs/          Published image assets, keyed by pack identifier.
schemas/               JSON Schemas the generated catalog must validate against.
scripts/
  common.py            Shared config/HTTP/workspace helpers.
  migrate_catalog.py   Stages A-E: fetch -> parse -> download -> validate-images -> generate.
  validate_catalog.py  Schema + cross-reference validation gate (run before publishing).
  publish_catalog.py   Stage F: commit + push the staged, validated catalog to GitHub.
migration-workspace/   Gitignored scratch space: raw API responses, downloaded images,
                       staged generated JSON, and all reports. Safe to delete and re-run.
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GITHUB_OWNER / GITHUB_REPOSITORY once the repo exists
```

## Running the pipeline

Each stage is restartable and writes its own report under `migration-workspace/reports/`.

```bash
# 1. Fetch raw pages 0-38 from the source API (tries the fallback host automatically).
python3 scripts/migrate_catalog.py fetch --pages 0-38 --resume

# 2. Normalize into a flat pack/sticker list.
python3 scripts/migrate_catalog.py parse

# 3. Download every tray/thumbnail/sticker image (already-valid files are skipped).
python3 scripts/migrate_catalog.py download --concurrency 8

# 4. Validate every downloaded image (format, dimensions, alpha, animation, SHA-256).
python3 scripts/migrate_catalog.py validate-images

# 5. Generate manifest.json / page JSON / pack-detail JSON with GitHub raw URLs.
python3 scripts/migrate_catalog.py generate

# 6. Combined migration-report.json / .md across all stages above.
python3 scripts/migrate_catalog.py report

# Or run 1-5 in one go:
python3 scripts/migrate_catalog.py all --pages 0-38
```

Add `--dry-run` to any stage to see what it would do without touching the network or disk state.

## Publishing

```bash
# Validate the staged output (also run automatically by publish_catalog.py):
python3 scripts/validate_catalog.py --catalog-dir migration-workspace/generated-catalog/catalog

# Dry-run: shows file counts, total size, largest file - no changes made.
python3 scripts/publish_catalog.py

# Actually copy into catalog/ + images/, commit, and push (never force-pushes):
python3 scripts/publish_catalog.py --yes
```

`publish_catalog.py` refuses to run until `validate_catalog.py` reports `READY`, refuses any file
over GitHub's 100MB hard limit, and verifies a sample of the pushed raw URLs with a plain HTTPS
HEAD request after pushing.

## Known constraints (see the implementation guide for full detail)

- Images are committed directly to this repo and served via `raw.githubusercontent.com` - **not**
  GitHub Pages (Pages caps published sites at 1GB; the estimated image set is ~3GB).
- The Android app must fetch individual raw URLs on demand and must never clone this repository.
- `GITHUB_TOKEN` (if ever needed) is read from the environment only - never written to a file or
  logged.
