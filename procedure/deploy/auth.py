"""Frictionless Snowflake authentication for the Chunky deploy toolchain.

Vendored from snowball's `sbcore/auth.py`. The mechanism is unchanged; the
read-only guard that wrapped every statement in snowball is not part of this
copy, because this toolchain exists to CREATE, PUT, CALL and DROP.

Mechanism
---------
Snowflake's built-in ``SNOWFLAKE$LOCAL_APPLICATION`` OAuth integration
(``authenticator=OAUTH_AUTHORIZATION_CODE``). It ships with every Snowflake
account -- no ``CREATE SECURITY INTEGRATION``, no keypair, no PAT, no admin
setup.

Observed behaviour (measured by snowball, not assumed):
  * access tokens live exactly 600s (JWT ``exp - iat``)
  * a valid cached access token   -> connects instantly, no browser
  * an expired access token       -> silent refresh-token grant, no browser
  * a missing/invalid refresh token -> one interactive browser login

Two defects are worked around here:

1. Windows Credential Manager cannot store a blob big enough for an OAuth
   token, so token caching is impossible with the stock keyring backend.
   Fixed by :mod:`winkeyring`, which MUST be installed before
   ``snowflake.connector`` is imported.

2. snowflake-connector-python mishandles the connection lifecycle during OAuth
   reauthentication: it refreshes the token and authenticates successfully,
   then closes a connection while a cursor still references it, surfacing as
   ``250002 (08003): Connection is closed``. Deterministic on the first call
   after the access token expires. :func:`connect` validates each new session
   and retries once, which fully absorbs it.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

from . import config
from .winkeyring import install as _install_keyring

_install_keyring()

import snowflake.connector  # noqa: E402  (must follow the keyring install)

_TRANSIENT_MARKERS = ("250002", "08003", "connection is closed")

# The browser leg of the OAuth flow can succeed -- caching a perfectly valid
# token -- while Snowflake still rejects the session because the identity
# behind that token is not the configured user. The tokens are written before
# the rejection, so without this the cache is left holding credentials for a
# login that provably does not work.
_IDENTITY_MISMATCH_MARKERS = ("differs from the user tied to the access token",)

_AUTH_FAILURE_MARKERS = (
    "incorrect username or password",
    "user is disabled",
    "user account is locked",
    "invalid oauth access token",
    "oauth access token expired",
)

_COMMAND_ENV = "CHUNKY_SF_COMMAND"
_RUN_ENV = "CHUNKY_SF_RUN_ID"


class AuthError(RuntimeError):
    pass


def query_tag(user: str) -> str:
    """Watermark every statement with the tool, its version, and the user.

    Makes Chunky's deploy traffic trivially attributable in QUERY_HISTORY --
    both for auditing what was deployed and for separating toolchain activity
    from the procedures' own statements (which carry their own tag).
    """
    from . import __version__

    parts = [f"ChunkyDeploy/{__version__}", f"user={user}"]
    command = os.environ.get(_COMMAND_ENV)
    if command:
        parts.append(f"cmd={command}")
    run_id = os.environ.get(_RUN_ENV)
    if run_id:
        parts.append(f"run={run_id}")
    return "|".join(parts)[:1900]


def _is_transient_reauth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def _is_identity_mismatch(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _IDENTITY_MISMATCH_MARKERS)


def _is_auth_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _AUTH_FAILURE_MARKERS)


def _purge_tokens(user: str, account: str) -> None:
    """Drop cached tokens so a failed login never looks like a working one."""
    try:
        logout(user, account)
    except Exception:
        pass


def _raw_connect(user: str, account: str, **overrides):
    params = dict(
        account=account,
        user=user,
        authenticator="OAUTH_AUTHORIZATION_CODE",
        client_store_temporary_credential=True,
        oauth_enable_refresh_tokens=True,
        session_parameters={"QUERY_TAG": query_tag(user)},
    )
    params.update(overrides)
    return snowflake.connector.connect(**params)


def connect(
    user: Optional[str] = None,
    account: Optional[str] = None,
    role: Optional[str] = None,
    warehouse: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = None,
    max_attempts: int = 2,
    quiet: bool = False,
    **overrides,
):
    """Open an authenticated session, absorbing the connector's reauth defect.

    Session context (role/warehouse/database/schema) is applied with USE
    statements after the connection validates, rather than as connect
    parameters, so a bad context produces a legible SQL error instead of an
    opaque authentication failure.
    """
    user = config.get_user(user)
    account = config.get_account(account)
    role = config.get_role(role)
    warehouse = config.get_warehouse(warehouse)
    database = config.get_database(database)
    schema = config.get_schema(schema)

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        conn = None
        try:
            conn = _raw_connect(user, account, **overrides)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            for stmt, value in (
                ("USE ROLE {}", role),
                ("USE WAREHOUSE {}", warehouse),
                ("USE DATABASE {}", database),
                ("USE SCHEMA {}", schema),
            ):
                if value:
                    conn.cursor().execute(stmt.format(_ident(value)))
            return conn
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last_exc = exc
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if _is_identity_mismatch(exc):
                _purge_tokens(user, account)
                raise AuthError(
                    f"Snowflake rejected '{user}'.\n"
                    f"The browser signed in successfully, but the identity behind "
                    f"that login is a different Snowflake user than the one "
                    f"configured here.\n"
                    f"Find the exact Snowflake username, then:\n"
                    f"  python procedure/deploy/sf.py config set --user <their-username>\n"
                    f"  python procedure/deploy/sf.py auth login\n"
                    f"Do not guess a second time. Cached tokens were cleared."
                ) from exc
            if _is_auth_failure(exc):
                _purge_tokens(user, account)
            if attempt < max_attempts and _is_transient_reauth_error(exc):
                if not quiet:
                    # stderr, not stdout: incidental progress chatter must not
                    # interleave with results that may be piped.
                    print(
                        f"  [auth] token refreshed mid-connect; retrying "
                        f"({attempt + 1}/{max_attempts})",
                        file=sys.stderr,
                    )
                continue
            raise
    raise last_exc  # pragma: no cover


def _ident(name: str) -> str:
    """Quote an identifier unless it is already qualified or quoted."""
    n = str(name).strip()
    if n.startswith('"') or "." in n:
        return n
    return '"' + n.replace('"', '""') + '"'


def session_info(conn) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), "
        "CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_ACCOUNT(), CURRENT_REGION()"
    )
    user, role, warehouse, database, schema, account, region = cur.fetchone()
    cur.close()
    return {
        "user": user,
        "role": role,
        "warehouse": warehouse,
        "database": database,
        "schema": schema,
        "account": account,
        "region": region,
    }


def login(**kwargs) -> dict:
    """Force a connection now so any required browser login happens up front."""
    started = time.time()
    conn = connect(**kwargs)
    try:
        info = session_info(conn)
        info["elapsed_seconds"] = round(time.time() - started, 2)
        return info
    finally:
        conn.close()


def logout(user: Optional[str] = None, account: Optional[str] = None) -> list:
    """Delete cached OAuth tokens, forcing a fresh browser login next time."""
    from snowflake.connector.token_cache import TokenCache, TokenKey, TokenType

    user = config.get_user(user)
    account = config.get_account(account)
    host = f"{account}.snowflakecomputing.com"

    cache = TokenCache.make()
    removed = []
    for token_type in (
        TokenType.OAUTH_ACCESS_TOKEN,
        TokenType.OAUTH_REFRESH_TOKEN,
        TokenType.ID_TOKEN,
        TokenType.MFA_TOKEN,
    ):
        try:
            key = TokenKey(user, host, token_type)
            if cache.retrieve(key) is not None:
                cache.remove(key)
                removed.append(token_type.value)
        except Exception:
            continue
    return removed


def _decode_jwt_payload(token: str):
    import base64
    import json

    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def token_status(user: Optional[str] = None, account: Optional[str] = None) -> dict:
    """Report cached-token state without opening a connection."""
    from snowflake.connector.token_cache import TokenCache, TokenKey, TokenType

    user = config.get_user(user)
    account = config.get_account(account)
    host = f"{account}.snowflakecomputing.com"
    cache = TokenCache.make()

    status = {"user": user, "account": account}
    access = cache.retrieve(TokenKey(user, host, TokenType.OAUTH_ACCESS_TOKEN))
    refresh = cache.retrieve(TokenKey(user, host, TokenType.OAUTH_REFRESH_TOKEN))

    status["refresh_token_cached"] = refresh is not None
    if access:
        claims = _decode_jwt_payload(access) or {}
        exp = claims.get("exp")
        status["access_token_cached"] = True
        status["access_token_expires_unix"] = exp
        if exp:
            remaining = int(exp - time.time())
            status["access_token_seconds_remaining"] = remaining
            status["access_token_expired"] = remaining <= 0
    else:
        status["access_token_cached"] = False

    if not status["refresh_token_cached"]:
        status["next_connect"] = "interactive browser login required"
    elif status.get("access_token_expired", True):
        status["next_connect"] = "silent refresh (no browser)"
    else:
        status["next_connect"] = "instant (cached access token)"
    return status


def oauth_access_token(user: Optional[str] = None,
                       account: Optional[str] = None,
                       refresh_if_expired: bool = True) -> Optional[str]:
    """Return a live OAuth access token, for the SQL API's Bearer header.

    The SQL API needs a bearer token, not a connector session. Rather than
    inventing a second credential, this reads the token the connector already
    cached during the browser flow. If it has expired (they live 600s), open a
    throwaway connection first -- that triggers the connector's silent
    refresh-token grant and rewrites the cache -- then re-read it.

    Returns None if no token is cached, in which case the caller should fall
    back to a PAT or a key-pair JWT.
    """
    from snowflake.connector.token_cache import TokenCache, TokenKey, TokenType

    user = config.get_user(user)
    account = config.get_account(account)
    host = f"{account}.snowflakecomputing.com"
    cache = TokenCache.make()
    key = TokenKey(user, host, TokenType.OAUTH_ACCESS_TOKEN)

    token = cache.retrieve(key)
    claims = _decode_jwt_payload(token) if token else None
    expired = True
    if claims and claims.get("exp"):
        expired = (claims["exp"] - time.time()) <= 30  # 30s safety margin

    if token and not expired:
        return token

    if not refresh_if_expired:
        return token

    try:
        conn = connect(user=user, account=account, quiet=True)
        conn.close()
    except Exception:
        return token
    return cache.retrieve(key)
