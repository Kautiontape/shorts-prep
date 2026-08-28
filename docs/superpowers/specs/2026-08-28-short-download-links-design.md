# Short Download Links — Design Spec

- **Date:** 2026-08-28
- **Status:** Approved (design); pending implementation plan
- **Author:** Shawn Squire (with Claude)

## Problem

After processing finishes, the result page renders a QR code (`app.py:845`)
containing the presigned R2 download URL returned by `/process/<job_id>`. That
URL is **491 characters**. Measured with the real signing code:

```
https://<32-hex-account>.r2.cloudflarestorage.com/shorts-prep/outputs/a1b2c3d4e5f6/my-vacation-clip-shorts.mp4
  ?response-content-disposition=attachment%3B%20filename%3D%22my-vacation-clip-shorts.mp4%22
  &X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=<key>%2F20260828%2Fauto%2Fs3%2Faws4_request
  &X-Amz-Date=20260828T205240Z
  &X-Amz-Expires=86400
  &X-Amz-SignedHeaders=host
  &X-Amz-Signature=<64 hex>
```

491 bytes needs **QR version 18 (89×89 modules)** even at the lowest error
correction level, which the page already uses (`correctLevel: L`). Rendered
into the 160×160 px canvas at `app.py:845`, each module is under 2 px. Phone
cameras cannot resolve it, so the QR handoff — the whole point of the feature —
does not work.

The length is inherent to SigV4: six `X-Amz-*` parameters plus a URL-encoded
`response-content-disposition`, of which the signature alone is 64 characters
of incompressible hex.

## Goal

Put a short URL in the QR code that redirects to the R2 download, without
adding a database or a second service.

## Non-goals

- **Compressing or encoding the presigned URL.** A SigV4 signature is 64 hex
  characters of hash output; it has no exploitable redundancy. No base62,
  Huffman, or dictionary scheme shrinks it meaningfully. Rejected.
- **A URL-shortener database.** Rejected — see below; the mapping already
  exists.
- **A separate short domain** (e.g. `ktq.re`). The domain is 25 of the 45
  characters in the new URL, so this is the largest remaining win, but 45
  characters already fits comfortably in a QR version 3. Revisit only if the
  QR is still hard to scan in practice.
- **Single-use / burn-after-download links.** This is the one variant that
  would genuinely require mutable per-code state. Not wanted.

## Key insight: no database is needed

`job_id` is already a unique, unguessable (12 hex chars ≈ 2^48) identifier
minted at `app.py:879`, and the R2 object key is already derived from it. A
redirect route can therefore rebuild the key from the code in the URL and mint
a fresh presigned URL on demand. The "shortener table" is the R2 keyspace.

The one value not derivable from `job_id` alone is the output filename, which
is currently embedded in the key (`outputs/{job_id}/{output_name}`,
`app.py:980`). The design removes that dependency by making the key
deterministic and storing the display name in a sidecar object.

## Chosen approach: re-signing redirect route

```
phone scans QR  ->  GET /d/a1b2c3d4e5f6
                      |- validate code against the existing JOB_ID_RE
                      |- GET  outputs/a1b2c3d4e5f6.name   -> "my-clip-shorts.mp4"
                      |- presign_get("outputs/a1b2c3d4e5f6.mp4", name, expires=300)
                      '- 302 -> R2 presigned URL
```

| | URL | chars | QR (ECC) |
|---|---|---|---|
| Today | R2 presigned GET | 491 | version 18, 89×89 (L) |
| New | `https://shorts.kautiontape.com/d/a1b2c3d4e5f6` | 45 | version 4, 33×33 (M) |

## Changes

### R2 key layout

`process` currently writes `outputs/{job_id}/{output_name}`. Because the
pipeline always produces MP4 (`build_ffmpeg_cmd` output is `.mp4`;
`output_name` is always `<stem>-shorts.mp4` or `shorts-ready.mp4`), the key can
be fully deterministic. Replace with two objects:

- `outputs/{job_id}.mp4` — the processed video
- `outputs/{job_id}.name` — the display filename, plain UTF-8 text

Both are covered by the existing lifecycle rule in `setup_r2.py`
(`Prefix: ''`, `Expiration: {Days: 1}`), and both are written at the same
moment, so they expire together. `setup_r2.py` needs no change.

### `r2.py`

Add `put_text(key, text)` and `get_text(key)` so the module remains the only
place that imports boto3. `get_text` propagates the boto3 not-found error to
the caller rather than swallowing it, so the route can distinguish "expired"
from other failures.

### `GET /d/<code>` route

- Validate `code` against the existing `JOB_ID_RE`; a non-match is treated the
  same as not found.
- Read the sidecar; a missing object means the lifecycle rule has reaped the
  job.
- Presign with `expires=300` and 302 to it.

The short expiry is deliberate. Because the URL is minted per visit, it only
has to survive the redirect. Today a 24-hour signed credential is printed into
a scannable code; afterwards the QR carries no credential at all.

### Error page

The global `HTTPException` handler at `app.py:19` returns JSON, which is wrong
for a route a phone browser lands on directly. `/d/<code>` returns its own HTML
response with status 404 — returned directly, **not** via `abort()`, so it does
not pass through that handler. Copy reads: this link has expired, files are
kept for 24 hours. Both a malformed code and a missing sidecar render the same
page; distinguishing them would leak whether a given code ever existed.

### `/process/<job_id>` response

Return `download_path='/d/<job_id>'` in place of `download_url`. The client
builds the absolute URL as `location.origin + result.download_path`
(`app.py:838`). Returning a relative path avoids introducing a
`PUBLIC_BASE_URL` env var and avoids depending on `X-Forwarded-Proto` being set
correctly through Cloudflare and nginx — the browser already knows its origin.

### Filename sanitizing

`presign_get` interpolates the name into `attachment; filename="..."`
(`r2.py:56`). A user filename containing a double quote or a control character
would corrupt that header — a latent bug today. Strip quotes, control
characters, and path separators when writing the sidecar, so the stored name is
already safe for every later reader.

### QR error correction

Raise `correctLevel` from `L` to `M` at `app.py:846`. `L` was necessary to fit
491 bytes at all; at 45 bytes there is ample room, and `M` scans far more
reliably off a screen. The code stays 33×33 versus today's 89×89.

## Testing

- `/d/<code>` with a valid code 302s to a URL built from `outputs/<code>.mp4`
  and the sidecar's download name.
- `/d/<code>` with a malformed code returns 404 with an HTML body.
- `/d/<code>` whose sidecar is missing returns 404 with an HTML body.
- Both 404 cases return byte-identical bodies.
- `/process/<job_id>` returns `download_path` and no `download_url`; replaces
  the existing assertion on `download_url`.
- `process` writes both `outputs/<job_id>.mp4` and `outputs/<job_id>.name`.
- A filename containing `"` is stored sanitized.
- `put_text`/`get_text` round-trip against a mocked client, matching the
  existing mocking style in `tests/test_r2.py`.

## Consequences

- Existing QR codes and links from before the deploy break. Acceptable: jobs
  live at most ~48 hours under the current lifecycle rule.
- The download now costs one extra round trip (a small R2 GET plus a redirect)
  before the transfer starts. Negligible against a multi-hundred-MB video.
- A short link works for as long as the object exists, rather than for a fixed
  24 hours from processing. To extend that, raise `Expiration.Days` in
  `setup_r2.py`; nothing in the app hard-codes a lifetime.
