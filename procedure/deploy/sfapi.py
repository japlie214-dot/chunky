"""Snowflake SQL API v2 client.

This is the transport the production API tier will use, so the toolchain
exercises it directly rather than only ever going through the Python
connector. Two behaviours matter and both are handled here:

1. **The 45-second rule.** If a statement runs longer than ~45s, or
   ``async=true`` was requested, the API answers HTTP 202 with a
   ``statementHandle`` instead of results. The client must then poll
   ``GET /api/v2/statements/{handle}`` until it returns 200 (done), or 422
   (failed). Every Chunky ingest is longer than 45s, so this is the normal
   path, not an edge case.

2. **A procedure's VARIANT comes back as a JSON *string*.** ``CALL`` returns
   one row, one column, and the SQL API renders that VARIANT as text. The
   caller has to ``json.loads`` it. :func:`call_procedure` does that.

Authentication -- three token types, in the order you would reach for them:

  * ``OAUTH``  -- the access token the connector already cached during the
    browser login (see :func:`auth.oauth_access_token`). Zero extra setup,
    which is why it is the default for development.
  * ``PROGRAMMATIC_ACCESS_TOKEN`` -- a PAT, for CI or a service.
  * ``KEYPAIR_JWT`` -- a JWT signed with the service user's private key. This
    is what a production API tier should use.

Standard library only -- no `requests` dependency.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import config

_API_PATH = "/api/v2/statements"


class SqlApiError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 body: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def resolve_token(token_type: Optional[str] = None,
                  token: Optional[str] = None,
                  user: Optional[str] = None,
                  account: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(token, token_type)`` for the Authorization header."""
    if token and token_type:
        return token, token_type.upper()

    pat = config.get_pat(token)
    if pat:
        return pat, "PROGRAMMATIC_ACCESS_TOKEN"

    key_path = config.get_private_key_path()
    if key_path:
        return keypair_jwt(key_path, user=user, account=account), "KEYPAIR_JWT"

    from . import auth

    oauth = auth.oauth_access_token(user=user, account=account)
    if oauth:
        return oauth, "OAUTH"

    raise SqlApiError(
        "No SQL API credential available.\n"
        "  Development:  run `sf auth login` first — the cached OAuth access "
        "token is reused automatically.\n"
        "  CI/service:   set CHUNKY_SF_PAT=<programmatic access token>, or set "
        "private_key_path in config.json for key-pair JWT."
    )


def keypair_jwt(private_key_path: str,
                user: Optional[str] = None,
                account: Optional[str] = None,
                lifetime_seconds: int = 3540) -> str:
    """Build a key-pair JWT for the SQL API.

    The subject is ``<ACCOUNT>.<USER>`` and the issuer additionally carries the
    SHA-256 fingerprint of the *public* key, which is how Snowflake matches the
    signature to the ``RSA_PUBLIC_KEY`` on the user. Account here is the
    account name WITHOUT the region/cloud suffix, uppercased.
    """
    import base64
    import hashlib

    from cryptography.hazmat.primitives import serialization

    user = (config.get_user(user) or "").upper()
    account = (config.get_account(account) or "").upper()
    # 'AB14212.AP-SOUTHEAST-1' -> 'AB14212'
    account = account.split(".", 1)[0].split("-", 1)[0] if "." in account else account

    with open(private_key_path, "rb") as fh:
        private_key = serialization.load_pem_private_key(fh.read(), password=None)

    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(public_der).digest()).decode()

    now = int(time.time())
    payload = {
        "iss": f"{account}.{user}.{fingerprint}",
        "sub": f"{account}.{user}",
        "iat": now,
        "exp": now + lifetime_seconds,
    }
    header = {"alg": "RS256", "typ": "JWT"}

    def b64(obj) -> bytes:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = b64(header) + b"." + b64(payload)

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _host(account: Optional[str] = None) -> str:
    return f"{config.get_account(account)}.snowflakecomputing.com"


def _request(method: str, url: str, token: str, token_type: str,
             body: Optional[dict] = None, timeout: int = 120) -> Tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Snowflake-Authorization-Token-Type", token_type)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "chunky-deploy/0.1")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------
def submit(statement: str,
           *,
           bindings: Optional[Dict[str, dict]] = None,
           timeout_seconds: int = 3600,
           asynchronous: bool = False,
           token: Optional[str] = None,
           token_type: Optional[str] = None,
           user: Optional[str] = None,
           account: Optional[str] = None,
           role: Optional[str] = None,
           warehouse: Optional[str] = None,
           database: Optional[str] = None,
           schema: Optional[str] = None) -> Tuple[int, dict, str, str]:
    """POST a statement. Returns ``(status, body, token, token_type)``."""
    token, token_type = resolve_token(token_type, token, user, account)

    url = f"https://{_host(account)}{_API_PATH}"
    if asynchronous:
        url += "?async=true"

    body: Dict[str, Any] = {
        "statement": statement,
        "timeout": timeout_seconds,
        "role": config.get_role(role),
        "warehouse": config.get_warehouse(warehouse),
        "database": config.get_database(database),
        "schema": config.get_schema(schema),
        "parameters": {"QUERY_TAG": f"ChunkyDeploy/sqlapi|req={uuid.uuid4().hex[:12]}"},
    }
    if bindings:
        body["bindings"] = bindings

    status, resp = _request("POST", url, token, token_type, body)
    return status, resp, token, token_type


def poll(handle: str, token: str, token_type: str,
         account: Optional[str] = None) -> Tuple[int, dict]:
    url = f"https://{_host(account)}{_API_PATH}/{handle}"
    return _request("GET", url, token, token_type)


def execute(statement: str,
            *,
            poll_seconds: float = 3.0,
            max_wait_seconds: int = 3600,
            on_poll=None,
            **kwargs) -> dict:
    """Submit and, if Snowflake defers it, poll until it finishes.

    Returns the final response body. Raises :class:`SqlApiError` on 4xx/5xx
    that is not the 202 deferral.
    """
    status, resp, token, token_type = submit(statement, **kwargs)

    if status == 200:
        return resp

    if status != 202:
        raise SqlApiError(
            f"SQL API returned HTTP {status}: "
            f"{resp.get('message') or resp.get('raw') or resp}",
            status=status, body=resp,
        )

    handle = resp.get("statementHandle")
    if not handle:
        raise SqlApiError("HTTP 202 with no statementHandle", status=202, body=resp)

    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        time.sleep(poll_seconds)
        attempt += 1
        status, resp = poll(handle, token, token_type,
                            account=kwargs.get("account"))
        if on_poll:
            on_poll(attempt, status, resp)
        if status == 200:
            return resp
        if status in (202, 429):
            continue
        raise SqlApiError(
            f"Statement {handle} failed with HTTP {status}: "
            f"{resp.get('message') or resp}",
            status=status, body=resp,
        )

    raise SqlApiError(
        f"Statement {handle} still running after {max_wait_seconds}s. "
        f"It is not cancelled — poll it with: sf api-status {handle}",
        status=202,
    )


def rows(resp: dict) -> List[List[Any]]:
    """The result rows of a completed response."""
    return resp.get("data") or []


def columns(resp: dict) -> List[str]:
    meta = (resp.get("resultSetMetaData") or {}).get("rowType") or []
    return [c.get("name", "") for c in meta]


def call_procedure(proc: str, command: str, instruction: dict, **kwargs) -> dict:
    """``CALL <proc>(?, PARSE_JSON(?))`` and unwrap the returned VARIANT.

    The instruction goes in as a TEXT binding into PARSE_JSON -- never
    concatenated into the SQL. The procedure's VARIANT return arrives as a
    JSON string in the first cell, so it is parsed back here.
    """
    statement = f"CALL {proc}(?, PARSE_JSON(?))"
    bindings = {
        "1": {"type": "TEXT", "value": command},
        "2": {"type": "TEXT", "value": json.dumps(instruction)},
    }
    resp = execute(statement, bindings=bindings, **kwargs)
    data = rows(resp)
    if not data or not data[0]:
        return {"success": False, "error": "Procedure returned no rows",
                "raw_response": resp}
    cell = data[0][0]
    if isinstance(cell, str):
        try:
            return json.loads(cell)
        except json.JSONDecodeError:
            return {"success": False, "error": "Return value was not JSON",
                    "raw": cell}
    return cell
