# mailserver-sidecar — Codebase Map

**Status (2026-11-15):** live, prod commit `94f858ac7810c80f713471b522ce87be90ebfb85`, deploy record `f9d4b24a-8842-4c31-bcb1-89b1f42df3c3`.

## Purpose

Thin FastAPI sidecar in front of a docker-mailserver instance on PROD2.
Lets other services (job-finder, stighive-character-agent,
stighive-digest, …) drive the mailserver over HTTP with per-caller
bearer tokens, so callers never speak IMAP or SMTP themselves and the
mailbox password only ever crosses one process boundary.

## Architecture

```
      caller (bearer)
            │  HTTPS
            ▼
    ┌────────────────┐    IMAP 993     ┌────────────────┐
    │ mailserver-    │────────────────▶│ docker-        │
    │ sidecar        │                 │ mailserver     │
    │ (FastAPI,      │    SMTP 587     │ (DMS)          │
    │  uvicorn)      │────────────────▶│                │
    └────────────────┘                 └────────────────┘
            │
            │  docker-exec (for /mailboxes)
            ▼
       /var/run/docker.sock  (host-mounted)
```

Single-process FastAPI app (`app.py`), served by `uvicorn` on port
`8080`. Auth, enforcement and IMAP/SMTP glue all live in the one
module; no DB, no queue, no background tasks. Image is built from this
repo's `Dockerfile` (python:3.11-slim).

## Entry points and routes

All auth'd routes take `Authorization: Bearer <raw>`. The raw key is
sha256'd on each call and looked up in `service_tokens.json`.

| Method | Path                | Scope    | Mailbox allowlist | Recipient allowlist |
| ------ | ------------------- | -------- | ----------------- | ------------------- |
| GET    | `/health`           | none     | —                 | —                   |
| GET    | `/`                 | none     | —                 | —                   |
| GET/POST | `/mailboxes`      | `read`   | n/a (no mailbox arg) | n/a              |
| POST   | `/fetch`            | `read`   | enforced          | —                   |
| POST   | `/attachments/bulk` | `read`   | enforced          | —                   |
| POST   | `/delete/bulk`      | `delete` | enforced          | —                   |
| POST   | `/send`             | `send`   | enforced          | enforced (to/cc/bcc + reply_to) |

Pydantic request models: `FetchRequest`, `BulkAttachmentsRequest`,
`BulkDeleteRequest`, `SendRequest` — all take the target `mailbox` and
its `password` in the body.

## Data stores

- **No database.** All server-side state lives on disk in two files
  bind-mounted into the container.
- **`service_tokens.json`** — the only mutable store. Keyed by
  `sha256(raw_key)`. Container writes `last_used_at` on every
  successful auth (see `_save_service_tokens`).
- **`mailbox_map.json`** — static, versioned in the image. Mailbox →
  `{persona, brand_name, character_id}`. `character_id` is intentionally
  null everywhere until stighive-character-runtime assigns real UUIDs
  (see its own `TODO(build)`).

## Env var NAMES (never values)

| Name                     | Purpose                                                                                   |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| `SERVICE_TOKENS_FILE`    | Path to the per-caller token file inside the container (default `/service_tokens.json`).  |
| `MAILBOX_MAP_FILE`       | Path to the mailbox→persona map (default `/app/mailbox_map.json`).                        |
| `IMAP_HOST`              | IMAP target host (default `mailserver`).                                                  |
| `IMAP_PORT`              | IMAP port (default `993`).                                                                |
| `IMAP_SSL`               | Whether to use IMAPS (default `true`).                                                    |
| `SMTP_HOST`              | SMTP target host (default: value of `IMAP_HOST`).                                         |
| `SMTP_PORT`              | SMTP port (default `587`).                                                                |
| `SMTP_STARTTLS`          | STARTTLS on submission (default `true`).                                                  |
| `ALLOWED_DOMAINS`        | Comma-separated list of sender domains (default `0239409423.com`). Empty disables gate.   |
| `ALLOWED_DOMAIN`         | Legacy singular form, still honored if `ALLOWED_DOMAINS` is unset.                        |
| `MAILSERVER_CONTAINER`   | Name of the DMS container for the docker-exec mailbox listing (default `mailserver`).     |
| `MAILSERVER_ACCOUNTS_FILE` | Fallback path to DMS `postfix-accounts.cf` if docker-exec isn't available.               |

## Deploy location and method

- **Host:** PROD2.
- **Stack dir:** `/opt/stacks/mailserver-sidecar/` (compose stack).
- **Repo checkout on host:** `/opt/stacks/mailserver-sidecar/repo` —
  image is built from a `git checkout` of the pinned commit, not pulled
  from a registry.
- **Current prod commit:** `94f858ac7810c80f713471b522ce87be90ebfb85`
  (per-token allowed_mailboxes / allowed_recipients on top of
  `4505188` multi-domain `ALLOWED_DOMAINS`).
- **Bind mounts:**
  - `/opt/stacks/mailserver-sidecar/service_tokens.json` → `/service_tokens.json` (rw, **single file** — see the README for the in-place edit procedure).
  - `/var/run/docker.sock` (ro) — needed for the docker-exec path in `/mailboxes`.
  - Optional: DMS config dir to `/mailserver-config` (ro) for the accounts-file fallback.
- **Ports:** `8080` published from the container; the sidecar has no
  public ingress — callers reach it on the private network (job-finder,
  character-agent, digest, …).
- **Deploy method:** git checkout at target commit, then `docker
  compose up -d --build`. No registry, no CI deploy.

## Cron and jobs

None in this repo. The sidecar is request-driven only. Cron that
*calls* the sidecar lives elsewhere (e.g. stighive-digest's 07:00
America/Toronto send on PROD2, record `f9d4b24a`).

## Auth model

- **Transport:** `Authorization: Bearer <raw>`.
- **At rest:** `service_tokens.json` keyed by `sha256(raw)` — raw keys
  never persisted. File is re-read per request, so flipping a `status`
  to anything other than `"active"` takes effect immediately without
  restart.
- **Scopes:** `read`, `send`, `delete`. Each route checks exactly one.
- **Per-token allowlists (new on `94f858ac`):**
  - `allowed_mailboxes` — case-insensitive exact-match list; if set,
    `payload.mailbox` must be in it on `/fetch`, `/attachments/bulk`,
    `/delete/bulk`, `/send`.
  - `allowed_recipients` — case-insensitive exact-match list; if set,
    every `to`/`cc`/`bcc` (and `reply_to` when present) on `/send` must
    be in it.
  - Absent field = no restriction. Existing tokens (job-finder, the
    character-agent read token) continue to work unchanged.
- **Error surface:** violations return `403` with a short `detail`.
  The 403 body never echoes the bearer or the mailbox password.
- **No rate limits, no audit trail** beyond `last_used_at` per token.
  Usage events are drafted as a future CR (CoS-CR-U2g,
  `aa3d851c-92a9-4b7e-b3c8-28859445dd45`) but not wired.

## Known open items

- **No real test run in CI.** Tests exist (`tests/test_allowlists.py`,
  13 cases incl. multi-domain interop) but there is no GitHub Action
  or hook that runs them on push. Local-only.
- **`_save_service_tokens` has no locking.** Full-file JSON overwrite
  per auth to stamp `last_used_at` — a host edit that lands
  concurrently with an in-flight request can clobber. In practice the
  window is tiny; `.bak` is the rollback.
- **`character_id` null across `mailbox_map.json`.** Gated on
  stighive-character-runtime assigning real UUIDs (see its Phase 1).
- **No usage events emitted.** CoS-CR-U2g drafted (`aa3d851c…`), not
  triggered.
- **Multi-tenant mail platform is future work.** CoS-CR-M1 drafted
  (`928bc650…`), not triggered. Current sidecar is single-tenant.
- **Direct-send ergonomics.** Callers still pass `password` in the
  request body. The go-live CR (`658c060f…`) raised a "send path that
  doesn't need the mailbox password" question; deferred.

## Graph records that matter

- **Standard (map currency):** `68987294-83d5-4052-bedf-ab21e0b0a787` —
  the rule this map exists to comply with.
- **Deploy block (go-live):** `d729ca49-017e-4708-8df6-6425ff048de3` —
  stighive-digest go-live block v1, where the `94f858ac` sidecar build
  is specified.
- **Deploy record:** `f9d4b24a-8842-4c31-bcb1-89b1f42df3c3` — closes
  out the go-live; records that the sidecar was built from a git
  checkout of `94f858ac`, that the `stighive-digest` token was added
  in place (send; `agent@stighive.com` only; Raf only), and that
  negative controls returned 403/403.
- **CR behind this commit:** `658c060f-fc53-45c7-8d7b-cb9d67176130` —
  CR for the send-scoped token and `stighive.com` sender for digest.
- **Spec (why digest needed the token):**
  `d3f604b1-dde9-438e-80c6-ce6361b22c74` — stighive-digest as Raf's
  single update channel; names the per-token allowlists as the small
  sidecar CR.
- **Prior implementation report (context for the auth model):**
  `dcd08fb6-c165-4331-9547-962308fbfffb` — the `2b32ef9` work that
  replaced the shared `API_KEY` with scoped per-caller tokens.
- **Drafted future CRs (not triggered):**
  - `aa3d851c-92a9-4b7e-b3c8-28859445dd45` — CoS-CR-U2g: emit usage
    events (sidecar as the single emitter for mail traffic).
  - `928bc650-e4bf-4b21-a8a1-5ddecbbab0e9` — CoS-CR-M1: multi-tenant
    mail platform on the control plane.
