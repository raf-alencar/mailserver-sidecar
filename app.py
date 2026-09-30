from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Tuple, Any
import imaplib
import email
from email import policy
from email.message import Message, EmailMessage
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, make_msgid, formatdate
import smtplib
import os
import base64
import hashlib
import json
from datetime import datetime, timezone

try:
    import docker as docker_sdk
except Exception:
    docker_sdk = None

app = FastAPI(title="mail-helper")

# Per-caller scoped tokens, replacing the old single shared API_KEY.
# File is keyed by sha256(raw key) so raw keys are never stored at rest;
# see require_scope() below. Not baked into the image (mutated at runtime
# for last_used_at) — expected to be bind-mounted from the deployment host.
SERVICE_TOKENS_FILE = os.getenv("SERVICE_TOKENS_FILE", "/service_tokens.json")

# Mailbox -> persona/brand metadata (character_id filled in once
# stighive-character-agent assigns real ids; see mailbox_map.json).
# Versioned in the image — not secret, just descriptive metadata.
MAILBOX_MAP_FILE = os.getenv("MAILBOX_MAP_FILE", "/app/mailbox_map.json")

IMAP_HOST = os.getenv("IMAP_HOST", "mailserver")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
IMAP_SSL = os.getenv("IMAP_SSL", "true").lower() == "true"

SMTP_HOST = os.getenv("SMTP_HOST", IMAP_HOST)
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

# Comma-separated list. ALLOWED_DOMAIN (singular) is still honored for existing deployments.
# An empty value disables the domain check.
ALLOWED_DOMAINS = [
    d.strip().lower()
    for d in os.getenv("ALLOWED_DOMAINS", os.getenv("ALLOWED_DOMAIN", "0239409423.com")).split(",")
    if d.strip()
]
MAILSERVER_CONTAINER = os.getenv("MAILSERVER_CONTAINER", "mailserver")

# Optional fallback if you mount DMS config into the sidecar:
#   - ./docker-data/dms/config:/mailserver-config:ro
#   - MAILSERVER_ACCOUNTS_FILE=/mailserver-config/postfix-accounts.cf
MAILSERVER_ACCOUNTS_FILE = os.getenv(
    "MAILSERVER_ACCOUNTS_FILE",
    "/mailserver-config/postfix-accounts.cf",
)


# -----------------------------
# Models
# -----------------------------

class FetchRequest(BaseModel):
    mailbox: str
    password: str
    since_uid: int = 0
    limit: int = 50
    include_text: bool = True
    only_with_attachments: bool = False


class AttachmentRef(BaseModel):
    uid: int
    part_id: str


class BulkAttachmentsRequest(BaseModel):
    mailbox: str
    password: str
    items: List[AttachmentRef] = Field(default_factory=list)


class BulkDeleteRequest(BaseModel):
    mailbox: str
    password: str
    uids: List[int] = Field(default_factory=list)


class SendRequest(BaseModel):
    mailbox: str
    password: str
    to: List[str] = Field(default_factory=list)
    subject: str
    text: Optional[str] = None
    html: Optional[str] = None
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    reply_to: Optional[str] = None


# -----------------------------
# Auth / validation
# -----------------------------

def _load_service_tokens() -> Dict[str, Any]:
    try:
        with open(SERVICE_TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_service_tokens(tokens: Dict[str, Any]) -> None:
    try:
        with open(SERVICE_TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
    except Exception:
        pass


def require_scope(request: Request, scope: str) -> str:
    """Validate the caller's bearer token and required scope.

    Tokens are looked up by sha256 hash (never stored raw) and re-read on
    every call so a revocation (status flipped to "revoked" in the file)
    takes effect immediately, with no redeploy needed. Returns the caller
    name on success.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_key = auth[len("Bearer "):]
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    tokens = _load_service_tokens()
    entry = tokens.get(key_hash)
    if not entry or entry.get("status") != "active":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if scope not in entry.get("scopes", []):
        raise HTTPException(status_code=403, detail=f"Token lacks '{scope}' scope")

    entry["last_used_at"] = datetime.now(timezone.utc).isoformat()
    tokens[key_hash] = entry
    _save_service_tokens(tokens)

    return entry.get("name", "unknown")


def load_mailbox_map() -> Dict[str, Any]:
    try:
        with open(MAILBOX_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def validate_mailbox(mailbox: str) -> None:
    if not mailbox or "@" not in mailbox:
        raise HTTPException(status_code=400, detail="Invalid mailbox")
    if ALLOWED_DOMAINS and mailbox.lower().rsplit("@", 1)[1] not in ALLOWED_DOMAINS:
        raise HTTPException(status_code=403, detail="Mailbox not allowed")


def validate_recipients(addresses: List[str], field_name: str) -> None:
    for addr in addresses:
        if not addr or "@" not in addr:
            raise HTTPException(status_code=400, detail=f"Invalid address in {field_name}")


def dedupe_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# -----------------------------
# IMAP helpers
# -----------------------------

def connect_imap(mailbox: str, password: str):
    try:
        if IMAP_SSL:
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        else:
            conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)

        login_typ, _ = conn.login(mailbox, password)
        if login_typ != "OK":
            raise HTTPException(status_code=401, detail="IMAP login failed")

        sel_typ, _ = conn.select("INBOX")
        if sel_typ != "OK":
            raise HTTPException(status_code=500, detail="Failed to select INBOX")

        return conn
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"IMAP connection failed: {str(e)}")


def close_imap(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass
    try:
        conn.logout()
    except Exception:
        pass


def imap_uid_search_since(conn, since_uid: int, limit: int) -> List[int]:
    start_uid = max(1, since_uid + 1)
    typ, data = conn.uid("SEARCH", None, f"UID {start_uid}:*")
    if typ != "OK":
        raise HTTPException(status_code=500, detail=f"UID SEARCH failed: {data}")

    raw = data[0].decode().strip() if data and data[0] else ""
    if not raw:
        return []

    uids = [int(x) for x in raw.split() if x.isdigit()]
    uids.sort()
    return uids[:limit]


def fetch_raw_message_by_uid(conn, uid: int) -> bytes:
    typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise HTTPException(status_code=500, detail=f"FETCH failed for UID {uid}: {data}")

    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])

    raise HTTPException(status_code=404, detail=f"Message content not found for UID {uid}")


# -----------------------------
# SMTP helpers
# -----------------------------

def send_via_smtp(
    mailbox: str,
    password: str,
    to: List[str],
    subject: str,
    text: Optional[str],
    html: Optional[str],
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
) -> Dict[str, Any]:
    cc = cc or []
    bcc = bcc or []

    try:
        msg = EmailMessage()
        msg["From"] = mailbox
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject.strip()
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=mailbox.split("@", 1)[1])

        if cc:
            msg["Cc"] = ", ".join(cc)

        if reply_to:
            msg["Reply-To"] = reply_to

        # Plain text first, then HTML alternative
        if text:
            msg.set_content(text)
        else:
            msg.set_content("This message contains HTML content.")

        if html:
            msg.add_alternative(html, subtype="html")

        all_recipients = to + cc + bcc

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            if SMTP_STARTTLS:
                server.starttls()
                server.ehlo()

            server.login(mailbox, password)
            server.send_message(msg, to_addrs=all_recipients)

        return {
            "ok": True,
            "from": mailbox,
            "to": to,
            "cc": cc,
            "bcc_count": len(bcc),
            "subject": subject.strip(),
            "message_id": msg["Message-ID"],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMTP send failed: {str(e)}")


# -----------------------------
# MIME helpers
# -----------------------------

def decode_mime_header(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def safe_datetime_to_iso(date_header: Optional[str]) -> Optional[str]:
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).isoformat()
    except Exception:
        return date_header


def get_text_bodies(msg: Message) -> Tuple[Optional[str], Optional[str]]:
    text_plain = None
    text_html = None

    for part in msg.walk():
        if part.is_multipart():
            continue

        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()

        # Skip file-like parts for body extraction
        if disposition == "attachment" or filename:
            continue

        ctype = (part.get_content_type() or "").lower()
        try:
            payload = part.get_content()
        except Exception:
            try:
                payload_bytes = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                payload = payload_bytes.decode(charset, errors="replace") if payload_bytes else None
            except Exception:
                payload = None

        if not payload:
            continue

        if ctype == "text/plain" and text_plain is None:
            text_plain = payload
        elif ctype == "text/html" and text_html is None:
            text_html = payload

    return text_plain, text_html


def enumerate_leaf_parts(msg: Message, prefix: str = "") -> List[Tuple[str, Message]]:
    parts: List[Tuple[str, Message]] = []

    if msg.is_multipart():
        children = msg.get_payload()
        if not isinstance(children, list):
            return parts

        for idx, child in enumerate(children, start=1):
            part_id = f"{prefix}.{idx}" if prefix else str(idx)
            if child.is_multipart():
                parts.extend(enumerate_leaf_parts(child, part_id))
            else:
                parts.append((part_id, child))
    else:
        parts.append((prefix or "1", msg))

    return parts


def is_attachment_part(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    filename = part.get_filename()

    if disposition == "attachment":
        return True
    if filename:
        return True
    if disposition == "inline" and filename:
        return True

    return False


def extract_attachments_metadata(msg: Message) -> List[Dict[str, Any]]:
    attachments = []

    for part_id, part in enumerate_leaf_parts(msg):
        if not is_attachment_part(part):
            continue

        filename = decode_mime_header(part.get_filename()) or "attachment"
        mime_type = part.get_content_type() or "application/octet-stream"

        try:
            payload = part.get_payload(decode=True) or b""
            size = len(payload)
        except Exception:
            size = None

        attachments.append({
            "part_id": part_id,
            "fileName": filename,
            "mimeType": mime_type,
            "size": size,
        })

    return attachments


def extract_attachment_content(msg: Message, target_part_id: str) -> Dict[str, Any]:
    for part_id, part in enumerate_leaf_parts(msg):
        if part_id != target_part_id:
            continue

        if not is_attachment_part(part):
            raise HTTPException(status_code=400, detail=f"Part {target_part_id} is not an attachment")

        filename = decode_mime_header(part.get_filename()) or "attachment"
        mime_type = part.get_content_type() or "application/octet-stream"
        payload = part.get_payload(decode=True) or b""

        return {
            "part_id": part_id,
            "fileName": filename,
            "mimeType": mime_type,
            "size": len(payload),
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }

    raise HTTPException(status_code=404, detail=f"Attachment part {target_part_id} not found")


def parse_message_metadata(uid: int, raw_bytes: bytes, include_text: bool = True) -> Dict[str, Any]:
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)

    text_plain, text_html = get_text_bodies(msg) if include_text else (None, None)
    attachments = extract_attachments_metadata(msg)

    return {
        "uid": uid,
        "messageId": decode_mime_header(msg.get("Message-Id")),
        "subject": decode_mime_header(msg.get("Subject")),
        "from": decode_mime_header(msg.get("From")),
        "to": decode_mime_header(msg.get("To")),
        "date": safe_datetime_to_iso(msg.get("Date")),
        "textPlain": text_plain,
        "textHtml": text_html,
        "hasAttachments": len(attachments) > 0,
        "attachments": attachments,
    }


# -----------------------------
# Mailserver helpers
# -----------------------------

def parse_mailboxes_from_output(output: str) -> List[str]:
    mailboxes = []

    for line in output.splitlines():
        line = line.strip()
        if not line or "@" not in line:
            continue

        # Handle plain lines or prefixed output
        tokens = line.replace(",", " ").split()
        email_token = next((t for t in tokens if "@" in t), None)
        if email_token:
            mailboxes.append(email_token.strip())

    return dedupe_preserve_order(mailboxes)


def list_mailserver_accounts_via_docker() -> List[str]:
    if docker_sdk is None:
        raise RuntimeError("docker SDK not installed in container")

    client = docker_sdk.from_env()
    container = client.containers.get(MAILSERVER_CONTAINER)

    # docker-mailserver helper command
    exec_result = container.exec_run("setup email list")
    output = exec_result.output.decode("utf-8", errors="replace").strip()

    if exec_result.exit_code != 0:
        raise RuntimeError(output or "Failed to list mailboxes via docker exec")

    mailboxes = parse_mailboxes_from_output(output)
    if mailboxes:
        return mailboxes

    raise RuntimeError("No mailboxes found in docker exec output")


def list_mailserver_accounts_via_file() -> List[str]:
    if not os.path.exists(MAILSERVER_ACCOUNTS_FILE):
        raise RuntimeError(f"Accounts file not found: {MAILSERVER_ACCOUNTS_FILE}")

    mailboxes = []
    with open(MAILSERVER_ACCOUNTS_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # docker-mailserver postfix-accounts.cf format is usually:
            # email@example.com|hashedpassword
            mailbox = line.split("|", 1)[0].strip()
            if "@" in mailbox:
                mailboxes.append(mailbox)

    mailboxes = dedupe_preserve_order(mailboxes)
    if mailboxes:
        return mailboxes

    raise RuntimeError("No mailboxes found in accounts file")


def list_mailserver_accounts() -> List[str]:
    errors = []

    try:
        return list_mailserver_accounts_via_docker()
    except Exception as e:
        errors.append(f"docker method failed: {str(e)}")

    try:
        return list_mailserver_accounts_via_file()
    except Exception as e:
        errors.append(f"file method failed: {str(e)}")

    raise HTTPException(
        status_code=500,
        detail=" ; ".join(errors) if errors else "Failed to list mailboxes",
    )


# -----------------------------
# Routes
# -----------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/mailboxes")
@app.post("/mailboxes")
def get_mailboxes(request: Request):
    require_scope(request, "read")

    addresses = list_mailserver_accounts()
    mailbox_map = load_mailbox_map()

    mailboxes = []
    for address in addresses:
        meta = mailbox_map.get(address, {})
        mailboxes.append({
            "address": address,
            "persona": meta.get("persona"),
            "brand_name": meta.get("brand_name"),
            "character_id": meta.get("character_id"),
        })

    return {
        "ok": True,
        "count": len(mailboxes),
        "mailboxes": mailboxes,
    }


@app.post("/fetch")
def fetch_messages(payload: FetchRequest, request: Request):
    require_scope(request, "read")
    validate_mailbox(payload.mailbox)

    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")

    conn = connect_imap(payload.mailbox, payload.password)
    try:
        uids = imap_uid_search_since(conn, payload.since_uid, payload.limit)
        messages = []

        for uid in uids:
            try:
                raw = fetch_raw_message_by_uid(conn, uid)
                item = parse_message_metadata(uid, raw, include_text=payload.include_text)

                if payload.only_with_attachments and not item["hasAttachments"]:
                    continue

                messages.append(item)
            except Exception as e:
                messages.append({
                    "uid": uid,
                    "error": str(e),
                })

        max_uid = max(
            [m["uid"] for m in messages if isinstance(m.get("uid"), int)],
            default=payload.since_uid
        )

        return {
            "ok": True,
            "mailbox": payload.mailbox,
            "since_uid": payload.since_uid,
            "max_uid": max_uid,
            "count": len(messages),
            "messages": messages,
        }

    finally:
        close_imap(conn)


@app.post("/attachments/bulk")
def bulk_attachments(payload: BulkAttachmentsRequest, request: Request):
    require_scope(request, "read")
    validate_mailbox(payload.mailbox)

    if not payload.items:
        return {
            "ok": True,
            "mailbox": payload.mailbox,
            "count": 0,
            "attachments": [],
        }

    conn = connect_imap(payload.mailbox, payload.password)
    try:
        by_uid: Dict[int, List[str]] = {}
        for item in payload.items:
            if item.uid < 1:
                raise HTTPException(status_code=400, detail="Invalid uid")
            by_uid.setdefault(item.uid, []).append(item.part_id)

        results = []

        for uid, part_ids in by_uid.items():
            try:
                raw = fetch_raw_message_by_uid(conn, uid)
                msg = email.message_from_bytes(raw, policy=policy.default)

                for part_id in part_ids:
                    try:
                        att = extract_attachment_content(msg, part_id)
                        results.append({
                            "uid": uid,
                            **att
                        })
                    except Exception as e:
                        results.append({
                            "uid": uid,
                            "part_id": part_id,
                            "error": str(e),
                        })
            except Exception as e:
                for part_id in part_ids:
                    results.append({
                        "uid": uid,
                        "part_id": part_id,
                        "error": str(e),
                    })

        return {
            "ok": True,
            "mailbox": payload.mailbox,
            "count": len(results),
            "attachments": results,
        }

    finally:
        close_imap(conn)


@app.post("/delete/bulk")
def bulk_delete(payload: BulkDeleteRequest, request: Request):
    require_scope(request, "delete")
    validate_mailbox(payload.mailbox)

    if not payload.uids:
        return {
            "ok": True,
            "mailbox": payload.mailbox,
            "requested": 0,
            "deleted_uids": [],
        }

    uids = sorted(set(int(uid) for uid in payload.uids if int(uid) > 0))
    if not uids:
        raise HTTPException(status_code=400, detail="No valid UIDs provided")

    conn = connect_imap(payload.mailbox, payload.password)
    try:
        seq_set = ",".join(str(uid) for uid in uids)

        store_typ, store_data = conn.uid("STORE", seq_set, "+FLAGS.SILENT", "(\\Deleted)")
        if store_typ != "OK":
            raise HTTPException(status_code=500, detail=f"UID STORE failed: {store_data}")

        expunge_typ, expunge_data = conn.expunge()
        if expunge_typ != "OK":
            raise HTTPException(status_code=500, detail=f"EXPUNGE failed: {expunge_data}")

        return {
            "ok": True,
            "mailbox": payload.mailbox,
            "requested": len(payload.uids),
            "deleted_uids": uids,
        }

    finally:
        close_imap(conn)


@app.post("/send")
def send_email(payload: SendRequest, request: Request):
    require_scope(request, "send")
    validate_mailbox(payload.mailbox)

    if not payload.to:
        raise HTTPException(status_code=400, detail="At least one recipient is required")

    validate_recipients(payload.to, "to")
    validate_recipients(payload.cc, "cc")
    validate_recipients(payload.bcc, "bcc")

    if payload.reply_to and "@" not in payload.reply_to:
        raise HTTPException(status_code=400, detail="Invalid reply_to address")

    if not payload.subject or not payload.subject.strip():
        raise HTTPException(status_code=400, detail="Subject is required")

    if not payload.text and not payload.html:
        raise HTTPException(status_code=400, detail="Either text or html is required")

    return send_via_smtp(
        mailbox=payload.mailbox,
        password=payload.password,
        to=payload.to,
        subject=payload.subject,
        text=payload.text,
        html=payload.html,
        cc=payload.cc,
        bcc=payload.bcc,
        reply_to=payload.reply_to,
    )


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "mail-helper",
        "routes": [
            "/health",
            "/mailboxes",
            "/fetch",
            "/attachments/bulk",
            "/delete/bulk",
            "/send",
        ],
    }
