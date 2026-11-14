"""Tests for per-token allowlists on /send plus mailbox endpoints.

Scenarios covered:
  - absent allowlists => unchanged behaviour (200, mocked SMTP)
  - mailbox + recipient allowlists honoured (200, mocked SMTP)
  - case-insensitivity of both allowlists
  - disallowed mailbox, to, cc, bcc, reply_to each => 403
  - mailbox allowlist applied to /fetch and /delete/bulk => 403 on disallow
  - multi-domain ALLOWED_DOMAINS interoperates with per-token allowlists

Real IMAP/SMTP is never touched: send_via_smtp is monkeypatched, and
the IMAP-backed endpoints short-circuit with 403 before any socket is
opened. Service tokens file is redirected to a tmp path per-test and
seeded by sha256(raw) so no raw keys are ever stored at rest.
"""

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Raw tokens exist only in-test. On disk, only their sha256 hashes are stored.
TOK_UNRESTRICTED = "test-unrestricted-raw"
TOK_RESTRICTED = "test-restricted-raw"
TOK_OTHERDOMAIN = "test-otherdomain-raw"


def _seed_tokens_file(path: Path) -> None:
    """Allowlist values use mixed case on purpose: enforcement must
    normalise, so disk values can be any case and still match lowercase
    payload values (and vice versa).
    """
    payload = {
        _sha256(TOK_UNRESTRICTED): {
            "name": "unrestricted",
            "status": "active",
            "scopes": ["send", "read", "delete"],
        },
        _sha256(TOK_RESTRICTED): {
            "name": "restricted",
            "status": "active",
            "scopes": ["send", "read", "delete"],
            "allowed_mailboxes": ["Agent@Example.Com"],
            "allowed_recipients": ["Raf@Example.ORG", "cc-ok@example.com"],
        },
        # Second mailbox lives on the other allowed domain. Lets the
        # multi-domain test verify that allowed_mailboxes layered on top
        # of a comma-separated ALLOWED_DOMAINS still works both ways.
        _sha256(TOK_OTHERDOMAIN): {
            "name": "otherdomain",
            "status": "active",
            "scopes": ["send"],
            "allowed_mailboxes": ["bot@other.org"],
            "allowed_recipients": ["dest@other.org"],
        },
    }
    path.write_text(json.dumps(payload))


def _fresh_app(tmp_path, monkeypatch, allowed_domains: str = ""):
    """Reload app with env applied so module-level globals re-evaluate.
    Allowlist checks run against a fresh module import per test so
    ALLOWED_DOMAINS/SERVICE_TOKENS_FILE pickups are deterministic.
    """
    tokens_file = tmp_path / "service_tokens.json"
    _seed_tokens_file(tokens_file)

    monkeypatch.setenv("SERVICE_TOKENS_FILE", str(tokens_file))
    monkeypatch.setenv("ALLOWED_DOMAINS", allowed_domains)
    monkeypatch.delenv("ALLOWED_DOMAIN", raising=False)

    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")

    def _fake_send(**kwargs):
        return {"ok": True, "from": kwargs.get("mailbox"), "mocked": True}

    monkeypatch.setattr(app_module, "send_via_smtp", _fake_send)

    from fastapi.testclient import TestClient

    return app_module, TestClient(app_module.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Default client: ALLOWED_DOMAINS empty so test addresses on example.com
    pass the domain gate and we can isolate allowlist behaviour.
    """
    _, c = _fresh_app(tmp_path, monkeypatch, allowed_domains="")
    return c


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _send_body(**overrides) -> dict:
    body = {
        "mailbox": "agent@example.com",
        "password": "irrelevant-mocked-smtp",
        "to": ["raf@example.org"],
        "subject": "hi",
        "text": "hello",
    }
    body.update(overrides)
    return body


# --- /send ------------------------------------------------------------------

def test_send_unrestricted_token_any_mailbox_any_recipient(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_UNRESTRICTED),
        json=_send_body(mailbox="anyone@example.com", to=["anywhere@example.net"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_send_restricted_token_happy_path_case_insensitive(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(
            mailbox="AGENT@example.com",
            to=["raf@EXAMPLE.org"],
            cc=["CC-OK@Example.com"],
            reply_to="Raf@example.org",
        ),
    )
    assert r.status_code == 200, r.text


def test_send_restricted_disallowed_mailbox(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(mailbox="other@example.com"),
    )
    assert r.status_code == 403
    assert "mailbox" in r.json()["detail"].lower()


def test_send_restricted_disallowed_to(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(to=["stranger@example.org"]),
    )
    assert r.status_code == 403
    assert "(to)" in r.json()["detail"]


def test_send_restricted_disallowed_cc(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(cc=["cc-bad@example.org"]),
    )
    assert r.status_code == 403
    assert "(cc)" in r.json()["detail"]


def test_send_restricted_disallowed_bcc(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(bcc=["sneak@example.org"]),
    )
    assert r.status_code == 403
    assert "(bcc)" in r.json()["detail"]


def test_send_restricted_disallowed_reply_to(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(reply_to="other@example.org"),
    )
    assert r.status_code == 403
    assert "reply_to" in r.json()["detail"]


def test_send_error_detail_never_echoes_bearer_or_password(client):
    r = client.post(
        "/send",
        headers=_auth(TOK_RESTRICTED),
        json=_send_body(mailbox="other@example.com", password="super-secret-pw-xyz"),
    )
    assert r.status_code == 403
    body_text = r.text
    assert TOK_RESTRICTED not in body_text
    assert "super-secret-pw-xyz" not in body_text


# --- /fetch and /delete/bulk (mailbox-only allowlist) -----------------------

def test_fetch_restricted_disallowed_mailbox_rejected_before_imap(client):
    r = client.post(
        "/fetch",
        headers=_auth(TOK_RESTRICTED),
        json={"mailbox": "other@example.com", "password": "x"},
    )
    assert r.status_code == 403


def test_delete_restricted_disallowed_mailbox_rejected_before_imap(client):
    r = client.post(
        "/delete/bulk",
        headers=_auth(TOK_RESTRICTED),
        json={"mailbox": "other@example.com", "password": "x", "uids": [1]},
    )
    assert r.status_code == 403


def test_unrestricted_mailbox_passes_allowlist_but_fails_on_imap(client):
    # Absent allowlist => mailbox check short-circuits; call then falls
    # through to IMAP (unreachable in test env). Status must NOT be 403
    # from the allowlist layer.
    r = client.post(
        "/fetch",
        headers=_auth(TOK_UNRESTRICTED),
        json={"mailbox": "anyone@example.com", "password": "x"},
    )
    assert r.status_code != 403


# --- ALLOWED_DOMAINS multi-domain interop ----------------------------------

def test_multi_domain_allowed_domains_still_gates_with_allowlists(tmp_path, monkeypatch):
    """ALLOWED_DOMAINS=example.com,other.org must co-exist with per-token
    allowlists: a mailbox outside both domains is a 403 from the domain
    gate; a mailbox in the second domain is accepted when the token's
    allowed_mailboxes also lists it; and a mailbox in the first domain
    is still refused when the token's allowed_mailboxes only covers the
    second. Covers the 4505188 multi-domain feature + this branch's
    per-token layer at the same time.
    """
    _, c = _fresh_app(tmp_path, monkeypatch, allowed_domains="example.com,other.org")

    # Second-domain mailbox, token scoped to that domain: 200.
    r = c.post(
        "/send",
        headers=_auth(TOK_OTHERDOMAIN),
        json={
            "mailbox": "bot@other.org",
            "password": "x",
            "to": ["dest@other.org"],
            "subject": "ok",
            "text": "hi",
        },
    )
    assert r.status_code == 200, r.text

    # Mailbox outside both domains: domain gate fires (403).
    r = c.post(
        "/send",
        headers=_auth(TOK_OTHERDOMAIN),
        json={
            "mailbox": "bot@elsewhere.net",
            "password": "x",
            "to": ["dest@other.org"],
            "subject": "ok",
            "text": "hi",
        },
    )
    assert r.status_code == 403

    # Mailbox in the FIRST allowed domain but NOT in this token's
    # allowed_mailboxes: per-token layer fires (403). Proves the two
    # gates stack rather than one swallowing the other.
    r = c.post(
        "/send",
        headers=_auth(TOK_OTHERDOMAIN),
        json={
            "mailbox": "bot@example.com",
            "password": "x",
            "to": ["dest@other.org"],
            "subject": "ok",
            "text": "hi",
        },
    )
    assert r.status_code == 403
