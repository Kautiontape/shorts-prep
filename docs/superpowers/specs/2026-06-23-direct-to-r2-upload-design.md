# Direct-to-R2 Upload — Design Spec

- **Date:** 2026-06-23
- **Status:** Approved (design); pending implementation plan
- **Author:** Shawn Squire (with Claude)

## Problem

Uploading a 250 MB video to `shorts.kautiontape.com` fails in two ways: the
progress bar sticks at ~1%, and a completed attempt returns an HTML error the
client reports as "Expected JSON, got `<`".

Root cause (confirmed by inspecting ktn): the host is proxied through
**Cloudflare**, which caps **request bodies at 100 MB** on Free/Pro plans
(200 MB Business; higher only on Enterprise). The 250 MB upload is rejected at
Cloudflare's edge **before** it reaches the origin. Evidence:

- `shorts.kautiontape.com` responds with `server: cloudflare` + `cf-ray`.
- nginx access log has **no** `/analyze` / 413 / 5xx entries for the attempts.
- gunicorn logs show no requests since boot — the app never saw the upload.
- nginx already allows `client_max_body_size 2G` and Flask sets
  `MAX_CONTENT_LENGTH = 2G`; both are irrelevant because the request dies
  one layer further out.

Cloudflare's 100 MB limit applies only to its **website proxy**. The **R2 S3
API endpoint** (`<account>.r2.cloudflarestorage.com`) is the storage API and
accepts up to **5 GB in a single PUT** — it is not subject to the proxy cap.

## Goal

Let users upload large videos (250 MB up to ~2 GB) and download the processed
result, by routing the file bytes **browser ↔ R2 directly** via presigned URLs,
so they never traverse the 100 MB-capped proxy. The app continues to do the
ffmpeg analysis/processing on a local copy.

## Non-goals

- Client-side (WASM) processing — rejected; the QR/phone handoff still needs an
  upload, and the processed file is also large.
- Chunked upload through Cloudflare — rejected in favor of R2.
- A job queue / separate worker (`Approach C`) — overkill for current scale;
  revisit only if traffic grows.

## Chosen approach: A — R2 for transfer, local temp for processing

Browser PUTs the file straight to R2. The app downloads the object to local
temp (R2→ktn egress is free), runs the **existing** ffmpeg pipeline unchanged,
uploads the result back to R2, and returns a presigned GET URL for download.
Only *how bytes get in and out* changes; *how they're processed* does not.

## Flow

```
1. POST /create-upload {filename}
     -> validate extension; create job_id (uuid hex 12)
     -> key = inputs/<job_id><ext>
     -> return { job_id, put_url (presigned PUT, ~15 min), key }

2. Browser: XHR PUT file -> put_url   (direct to R2)
     -> progress bar driven by xhr.upload.progress
     -> stall watchdog: abort if no progress for ~30s

3. POST /analyze {job_id, filename}
     -> download R2 object to UPLOAD_DIR/<job_id>/input<ext>
     -> probe_detailed + run_checks + recommend_mode  (unchanged)
     -> delete R2 input object (local copy is now the working copy)
     -> return { id, filename, file_size_mb, checks, recommended_mode, bsf_available }

4. POST /process/<job_id> {mode}
     -> build_ffmpeg_cmd(mode, local_input, local_output)  (unchanged)
     -> upload output to R2 key outputs/<job_id>/<output_name>
     -> presign GET (24h) for the output
     -> delete local temp (input + output)
     -> return { id, output_name, mode_used, before_checks, after_checks,
                 file_size_mb, download_url }

5. Result panel + QR use download_url (presigned GET) directly.
```

## Components & file changes

### New: `r2.py`
Single-purpose module. Builds one boto3 S3 client from env vars; nothing else
in the codebase imports boto3.

- `presign_put(key, content_type, expires=900) -> url`
- `presign_get(key, download_name, expires=86400) -> url`
- `download_to(key, local_path)`
- `upload_file(local_path, key, content_type)`
- `delete(key)`

Config from env: `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`.
Real values live in `.credentials` (local, gitignored) and `.env` (on ktn,
gitignored) — never committed. Bucket: `shorts-prep`.

### `app.py`
- **Add** `POST /create-upload` — validate `.mov`/`.mp4`, return presigned PUT
  + job_id + key.
- **Rewrite** `POST /analyze` — accept JSON `{job_id, filename}` (not a file
  body); download from R2 to local temp, then run the existing probe/checks.
- **Adjust** `POST /process/<job_id>` — after the existing ffmpeg step, upload
  the result to R2 and return a presigned GET `download_url`; delete local temp.
- **Remove** `GET /download/<job_id>` — replaced by the presigned GET URL.
- **Remove** `MAX_CONTENT_LENGTH = 2G` — the app no longer receives file bodies.
- **Add** Flask `@app.errorhandler`s (404/413/500/Exception) returning JSON, so
  the app never emits an HTML error page.
- **Unchanged:** `probe_detailed`, `run_checks`, `recommend_mode`,
  `build_ffmpeg_cmd`, `check_faststart`, `cleanup_old_jobs`, `MODE_FIXES`,
  `STANDARD_FRAMERATES`, `BSF_AVAILABLE`.

### Frontend JS (inside `app.py`)
- `analyzeFile(file)` becomes: `POST /create-upload` → XHR `PUT` to R2 (with
  progress) → `POST /analyze {job_id, filename}`.
- **Robust response parsing:** read response as text, attempt `JSON.parse`; on
  failure show `HTTP <status>: <body excerpt>` instead of throwing — so the real
  response is always visible.
- **Stall watchdog:** during the PUT, if `upload.progress` doesn't advance for
  ~30 s, abort and show "upload stalled".
- Result download button + QR use `download_url` (presigned GET).

### `Dockerfile`
- Add `boto3` to the `pip install` line.

### `docker-compose.yml`
- Add `env_file: .env`. The `.env` lives at `/opt/services/shorts-prep/.env` on
  ktn (gitignored), holding the four `R2_*` vars.

### R2 bucket config (one-time, applied via boto3 with the existing keys)
- **CORS:** allow `PUT` from origin `https://shorts.kautiontape.com`; allow the
  `content-type` request header; expose `ETag`. (GET is a top-level
  navigation/QR open, not XHR, so it needs no CORS.)
- **Lifecycle:** expire `inputs/` and `outputs/` objects after 1 day, as a
  backstop to the app-side deletes.

## Error handling

| Stage | Failure | Behavior |
|-------|---------|----------|
| `/create-upload` | bad extension | JSON 400 |
| Browser PUT → R2 | non-2xx | show `HTTP <status>` + body excerpt |
| Browser PUT → R2 | stalled | watchdog aborts after ~30 s, clear message |
| `/analyze` | R2 download fails | JSON 502 "couldn't fetch upload" |
| `/analyze` | probe fails | existing behavior |
| `/process` | ffmpeg fails | existing stderr-tail JSON 500 |
| `/process` | R2 upload fails | JSON 502 |
| any app route | unhandled | `@app.errorhandler` → JSON, never HTML |

## Testing

- **TDD for `r2.py`** — unit tests with mocked S3 (`moto`): presign returns a
  URL; download/upload roundtrip; delete. Written before implementation.
- **Endpoint tests** — `/create-upload` returns presigned URL + job_id;
  `/analyze` and `/process` with R2 mocked; error handlers return JSON.
- **Manual end-to-end** — after deploy, push a real >100 MB file through
  `shorts.kautiontape.com`: confirm the PUT bypasses the cap and
  analyze/process/QR-download all work.

## Security

- Secrets only in env; never committed (`.credentials` and `.env` gitignored).
- Presigned lifetimes: PUT 15 min, GET 24 h.
- Object keys use an unguessable `job_id` (uuid hex 12).
- R2 input deleted after successful download; lifecycle rule as backstop.
- The R2 credentials were shared in plaintext during design; rotating them after
  implementation is advisable (owner's call).

## Decisions / defaults

- Presigned GET lifetime **24 h** (vs the old 2 h local cleanup) so the phone
  handoff isn't time-pressured.
- CORS + lifecycle applied **via boto3** using the existing keys (not the
  dashboard).
- `/download` **removed** entirely rather than kept as a redirect.
