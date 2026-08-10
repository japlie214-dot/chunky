# `sf` — the Chunky deploy CLI

snowball's authentication, with writes enabled.

`snowball` is the right tool for *reading* Snowflake and the wrong tool for
*building* it: `sbcore/sqlguard.py` rejects every DDL and DML statement by
design. Chunky's toolchain has to `CREATE PROCEDURE`, `PUT` a bundle, `CALL`
procedures that write, and `DROP` test objects. So this is a small copy of
snowball's auth layer with the guard removed and nothing else brought along —
no DuckDB, no charts, no ledger.

**Verified working on this machine**, with no browser prompt — it reuses the
refresh token snowball already cached:

```
$ sf auth whoami
  user : MCDAAI_DNA   role : IT_AI   warehouse : SHARED_ID_XS
  database : SBOX_DB  schema : AI_SB  region : AWS_AP_SOUTHEAST_1

$ sf api "SELECT CURRENT_ROLE() AS R, CURRENT_WAREHOUSE() AS W"
  R      W
  IT_AI  SHARED_ID_XS
```

The second one is the SQL API v2 — the transport the production API tier will
use — authenticating with the cached OAuth access token. No PAT, no key pair,
no service user needed to start testing against it.

---

## Install

```powershell
cd C:\Users\10102721\Documents\ClaudeFiles\chunky
py -3.11 -m venv .venv-proc
.\.venv-proc\Scripts\Activate.ps1
python -m pip install "snowflake-connector-python[secure-local-storage]>=3.16" pytest pypdf
```

`[secure-local-storage]` pulls in `keyring`. Without it, OAuth token caching
on Windows is impossible and every single command opens a browser.

## Configure

```powershell
Copy-Item procedure\deploy\config.example.json procedure\deploy\config.json
python procedure\deploy\sf.py config show
```

`config show` prints every effective value *and which tier produced it*, so
you can tell whether editing `config.json` will actually change anything or
whether an env var is shadowing it.

Resolution order: `--flag` → env (`CHUNKY_SF_*`, then `SNOWFLAKE_*`) →
`procedure/deploy/config.json` → `~/.snowball/config.json` (user/account only)
→ built-in default. There is no default `user` — guessing produces a browser
login that succeeds against the wrong identity.

## Use

```powershell
# auth
python procedure\deploy\sf.py auth status      # cached-token state, no connection
python procedure\deploy\sf.py auth login       # browser, once per refresh-token lifetime
python procedure\deploy\sf.py auth whoami      # connect and print the session context
python procedure\deploy\sf.py auth logout      # clear cached tokens

# SQL (DDL and DML allowed)
python procedure\deploy\sf.py sql "SELECT CURRENT_ROLE()"
python procedure\deploy\sf.py sql "SHOW STAGES IN SCHEMA SBOX_DB.AI_SB" --format json
python procedure\deploy\sf.py sql - < some_query.sql

# a whole .sql file, statement by statement
python procedure\deploy\sf.py script procedure\build\out\chunky_ingest.sql --dry-run
python procedure\deploy\sf.py script procedure\build\out\chunky_ingest.sql

# stages
python procedure\deploy\sf.py ls  "@SBOX_DB.AI_SB.CHUNKY_UTILS"
python procedure\deploy\sf.py put procedure\build\out\utils_bundle_v2.0.0.zip "@SBOX_DB.AI_SB.CHUNKY_UTILS"
python procedure\deploy\sf.py get "@SBOX_DB.AI_SB.DOCS/docs/report.pdf" .\downloads

# call a Chunky procedure and get its JSON back, unwrapped
python procedure\deploy\sf.py call CHUNKY_INGEST status '{"ping": true}'
python procedure\deploy\sf.py call CHUNKY_INGEST ingest @jobs\ingest_smoke.json

# the same, over the SQL API v2 — this is the transport the production API
# tier uses, including the HTTP 202 -> poll path that every ingest hits
python procedure\deploy\sf.py api "SELECT CURRENT_ROLE()"
python procedure\deploy\sf.py api-call CHUNKY_INGEST ingest @jobs\ingest_smoke.json
python procedure\deploy\sf.py api-status <statementHandle>
```

`call` and `api-call` accept the instruction as inline JSON, `@path/to.json`,
or `-` for stdin. Both exit non-zero when the procedure returns
`success: false`, so they compose in a script.

## Why each piece exists

| Module | Why it is not simpler |
|---|---|
| `winkeyring.py` | Windows Credential Manager caps one credential blob at 2560 bytes and stores it as UTF-16. OAuth tokens are bigger, so the stock keyring backend dies with `(1783, 'CredWrite', 'The stub received bad data')` and the connector silently cannot cache anything. This splits the secret across several entries. Verbatim from snowball. `install()` must run **before** `import snowflake.connector`. |
| `auth.py` | `authenticator=OAUTH_AUTHORIZATION_CODE` against the built-in `SNOWFLAKE$LOCAL_APPLICATION` integration — no security integration to create, no keypair, no admin. Access tokens live 600 s and refresh silently. The connect loop validates each new session and retries once, which absorbs a connector defect (`250002 (08003): Connection is closed`) that fires deterministically on the first call after a token expires. It also detects the case where the browser login succeeds but Snowflake rejects the session because the SSO identity is a *different* Snowflake user — it purges the poisoned cache instead of leaving `auth status` cheerfully reporting health. |
| `sqlsplit.py` | Every procedure DDL in this repo wraps a Python body in `$$ ... $$`, and those bodies contain semicolons. `script.split(";")` shreds them into unrunnable fragments and Snowflake reports the error on the wrong line. This tracks `$$`, `'`, `"`, `--` and `/* */`. |
| `sfapi.py` | The SQL API returns HTTP 202 + a `statementHandle` for anything over ~45 s. Every Chunky ingest is over 45 s, so the poll loop is the normal path. It also unwraps the procedure's VARIANT, which the API renders as a JSON *string* in the first cell — a detail that trips up every new client once. |
| `config.py` | Reads `user`/`account` from `~/.snowball/config.json` as a last resort, so an identity already set up on this machine does not have to be configured twice. |

## Credentials for the SQL API

`sf api*` needs a bearer token, and picks one in this order:

1. `CHUNKY_SF_PAT` env var → `PROGRAMMATIC_ACCESS_TOKEN`
2. `private_key_path` in config → `KEYPAIR_JWT` (signed locally with `cryptography`)
3. the OAuth access token the connector already cached → `OAUTH`

(3) means `sf api` works right after `sf auth login` with zero extra setup,
which is what you want in development. Production should use (1) or (2) with
a dedicated service user — that decision is Open Decision #4 in
[`../plan.md`](../plan.md).

Nothing here writes a token to disk itself; the connector's own cache and the
Windows Credential Manager hold everything.

## Guard rails

There are none, deliberately — this tool writes. What it does have:

- Every statement carries `QUERY_TAG = ChunkyDeploy/<version>|user=..|cmd=..`,
  so toolchain traffic is separable from the procedures' own statements and
  from human activity in `QUERY_HISTORY`.
- `sf script` stops at the first failed statement unless `--keep-going`, and
  prints the failing statement's `query_id` for follow-up.
- `sf script --dry-run` shows exactly what will run.
