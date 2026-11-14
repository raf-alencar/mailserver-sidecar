# mailserver-sidecar

FastAPI sidecar in front of a docker-mailserver instance. Exposes thin
auth'd routes (`/mailboxes`, `/fetch`, `/attachments/bulk`, `/delete/bulk`,
`/send`) for other services to drive the mailserver without speaking
IMAP/SMTP themselves.

## service_tokens.json

Per-caller bearer tokens. The file is keyed by `sha256(raw_key)` — raw
keys are never persisted. Re-read on every request, so flipping a `status`
takes effect immediately (no restart). The container writes
`last_used_at` back after every successful auth, so the mount must be
writable.

### Entry format

Each top-level key is the sha256 (hex) of the raw bearer. Required fields:
`name`, `status`, `scopes`. All other fields are optional.

| Field                | Type          | Required | Meaning                                                                                                   |
| -------------------- | ------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `name`               | string        | yes      | Human label for the caller (shown in logs / future audit).                                                |
| `status`             | string        | yes      | `"active"` to accept; anything else (e.g. `"revoked"`) rejects with 401.                                  |
| `scopes`             | list[string]  | yes      | Any of `read`, `send`, `delete`. Each endpoint checks one scope.                                          |
| `allowed_mailboxes`  | list[string]  | no       | If set, the request's `mailbox` must be in this list (case-insensitive exact match). Enforced on `/fetch`, `/attachments/bulk`, `/delete/bulk`, `/send`. **Absent ⇒ no restriction.** |
| `allowed_recipients` | list[string]  | no       | If set, every address in `to`/`cc`/`bcc` (and `reply_to` when present) on `/send` must be in this list (case-insensitive exact match). **Absent ⇒ no restriction.** |
| `last_used_at`       | string (ISO)  | auto     | Written by the container on every successful auth. Do not set by hand.                                    |

Absent allowlist fields preserve existing behaviour — existing tokens
(e.g. the job-finder entry) work unchanged.

### Example entry (placeholder values only)

```jsonc
{
  "0000000000000000000000000000000000000000000000000000000000000000": {
    "name": "example-caller",
    "status": "active",
    "scopes": ["send", "read"],
    "allowed_mailboxes": ["agent@example.com"],
    "allowed_recipients": ["recipient@example.org"]
  }
}
```

### Enforcement summary

| Endpoint             | Scope  | `allowed_mailboxes` | `allowed_recipients` |
| -------------------- | ------ | ------------------- | -------------------- |
| `GET/POST /mailboxes`| read   | n/a (no mailbox arg) | n/a                 |
| `POST /fetch`        | read   | enforced            | n/a                  |
| `POST /attachments/bulk` | read | enforced          | n/a                  |
| `POST /delete/bulk`  | delete | enforced            | n/a                  |
| `POST /send`         | send   | enforced            | enforced (to/cc/bcc + reply_to) |

A violation returns `403` with a short `detail` string. The 403 response
never echoes the bearer or the mailbox password.

### Adding an entry on PROD2 (single-file bind mount)

On PROD2 the file is bind-mounted as a **single file**, not a directory:
`/opt/stacks/mailserver-sidecar/service_tokens.json` → `/service_tokens.json`
(rw). An atomic `mv`/rename on the host replaces the host inode, but the
container keeps the old inode open — so the new entry would appear on
the host while the container still sees the pre-rename file. Edit
in-place through the same inode instead:

1. Back up: `cp /opt/stacks/mailserver-sidecar/service_tokens.json /opt/stacks/mailserver-sidecar/service_tokens.json.bak.$(date +%s)`
2. Generate a raw secret (don't commit it, share out of band):
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
3. Hash it: `python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" '<raw>'`
4. Add the entry under that hash using an in-place edit that preserves
   the inode — e.g. `python3`:
   ```
   python3 - <<'PY'
   import json, pathlib
   p = pathlib.Path("/opt/stacks/mailserver-sidecar/service_tokens.json")
   data = json.loads(p.read_text())
   data["<sha256-hex>"] = {
       "name": "<caller-name>",
       "status": "active",
       "scopes": ["send"],
       "allowed_mailboxes": ["<mailbox>"],
       "allowed_recipients": ["<recipient>"],
   }
   # open r+ / truncate writes through the same inode the container has open.
   with p.open("r+") as f:
       f.seek(0); f.truncate()
       json.dump(data, f, indent=2)
   PY
   ```
   `sed -i` on Linux also edits in place without changing the inode and
   is acceptable; `mv new old` is **not**.
5. Verify: `curl -H "Authorization: Bearer <raw>" http://<host>:<port>/health`
   then a scoped call against the sidecar.

No container restart is needed — `_load_service_tokens` re-reads per
request. One caveat: `_save_service_tokens` (called on every successful
auth to stamp `last_used_at`) does a full `json.dump` overwrite with no
locking, so a host edit that lands concurrently with an in-flight
request can race. Low risk in practice because the window is tiny and
the file is small, but the `.bak` from step 1 is your rollback if it
happens.

## Tests

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m pytest tests -v
```

Tests cover: absent allowlists (unchanged behaviour), happy-path 200
with mocked SMTP, case-insensitivity, 403 for disallowed mailbox / to /
cc / bcc / reply_to, and multi-domain `ALLOWED_DOMAINS` interop with
per-token allowlists.
