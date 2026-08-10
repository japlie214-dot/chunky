"""Configuration resolution for the Chunky deploy toolchain.

Precedence (highest first):
    1. explicit CLI flag
    2. environment variable  (CHUNKY_SF_* , then SNOWFLAKE_*)
    3. procedure/deploy/config.json          (this repo, git-ignored)
    4. ~/.snowball/config.json               (the identity already set up on this machine)
    5. built-in default

Tier 4 is the "reuse the auth" requirement made literal: whoever already ran
`sb auth setup` on this machine has a working Snowflake identity, and there is
no reason to make them configure it a second time. Only `user` and `account`
are read from there -- database/schema/warehouse deliberately are not, because
snowball's defaults point at the analyst's working area, not at Chunky's
sandbox.

There is no default `user`. Snowflake's connect() does not need one to reach
the account, but it does need one to authenticate, and guessing produces a
browser login that succeeds against the wrong identity -- see auth.py's
identity-mismatch handling.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple

# Fixed for this deployment.
ACCOUNT = "ab14212.ap-southeast-1"

# The sandbox this plan builds and tests in.
DEFAULT_DATABASE = "SBOX_DB"
DEFAULT_SCHEMA = "AI_SB"
DEFAULT_WAREHOUSE = "SHARED_ID_XS"

# The role that OWNS the procedures. Callers of the deployed procedures are
# NOT expected to hold it -- the procedures are EXECUTE AS CALLER and run with
# whatever role the caller brings. This value is only the role the deploy
# toolchain assumes in order to create objects.
DEFAULT_ROLE = "IT_AI"

DEPLOY_DIR = Path(__file__).resolve().parent
CONFIG_PATH = DEPLOY_DIR / "config.json"
SNOWBALL_CONFIG_PATH = Path(os.path.expanduser("~")) / ".snowball" / "config.json"


class ConfigError(RuntimeError):
    pass


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_file() -> dict:
    return _load_json(CONFIG_PATH)


def _load_snowball() -> dict:
    return _load_json(SNOWBALL_CONFIG_PATH)


def save_config(**values) -> Path:
    current = _load_file()
    current.update({k: v for k, v in values.items() if v is not None})
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return CONFIG_PATH


def _resolve_with_source(
    cli_value: Any,
    env_names: Tuple[str, ...],
    file_key: str,
    default: Any = None,
    *,
    from_snowball: bool = False,
) -> Tuple[Any, str]:
    if cli_value:
        return cli_value, "--flag"
    for name in env_names:
        val = os.environ.get(name)
        if val:
            return val, f"env:{name}"
    val = _load_file().get(file_key)
    if val:
        return val, "config.json"
    if from_snowball:
        val = _load_snowball().get(file_key)
        if val:
            return val, "~/.snowball/config.json"
    return default, "default"


def _resolve(cli_value, env_names, file_key, default=None, *, from_snowball=False):
    return _resolve_with_source(
        cli_value, env_names, file_key, default, from_snowball=from_snowball
    )[0]


def get_user(cli_user: Optional[str] = None) -> str:
    user = _resolve(
        cli_user,
        ("CHUNKY_SF_USER", "SNOWFLAKE_USER", "SNOWBALL_USER"),
        "user",
        None,
        from_snowball=True,
    )
    if not user:
        raise ConfigError(
            "No Snowflake user configured.\n"
            "  Set it once with:  python procedure/deploy/sf.py config set --user <you>\n"
            "  or per-run with:   --user <you>\n"
            "  or via env:        CHUNKY_SF_USER=<you>"
        )
    return user


def get_account(cli_account: Optional[str] = None) -> str:
    return _resolve(
        cli_account,
        ("CHUNKY_SF_ACCOUNT", "SNOWFLAKE_ACCOUNT", "SNOWBALL_ACCOUNT"),
        "account",
        ACCOUNT,
        from_snowball=True,
    )


def get_role(cli_role: Optional[str] = None) -> Optional[str]:
    return _resolve(cli_role, ("CHUNKY_SF_ROLE", "SNOWFLAKE_ROLE"), "role", DEFAULT_ROLE)


def get_warehouse(cli_wh: Optional[str] = None) -> Optional[str]:
    return _resolve(
        cli_wh, ("CHUNKY_SF_WAREHOUSE", "SNOWFLAKE_WAREHOUSE"), "warehouse", DEFAULT_WAREHOUSE
    )


def get_database(cli_db: Optional[str] = None) -> Optional[str]:
    return _resolve(cli_db, ("CHUNKY_SF_DATABASE",), "database", DEFAULT_DATABASE)


def get_schema(cli_schema: Optional[str] = None) -> Optional[str]:
    return _resolve(cli_schema, ("CHUNKY_SF_SCHEMA",), "schema", DEFAULT_SCHEMA)


def get_lib_stage(cli_stage: Optional[str] = None) -> str:
    return _resolve(
        cli_stage, ("CHUNKY_SF_LIB_STAGE",), "lib_stage",
        f"@{get_database()}.{get_schema()}.CHUNKY_UTILS",
    )


def get_docs_stage(cli_stage: Optional[str] = None) -> str:
    return _resolve(
        cli_stage, ("CHUNKY_SF_DOCS_STAGE",), "docs_stage",
        f"@{get_database()}.{get_schema()}.CHUNKY_DOCS",
    )


def get_render_stage(cli_stage: Optional[str] = None) -> str:
    return _resolve(
        cli_stage, ("CHUNKY_SF_RENDER_STAGE",), "render_stage",
        f"@{get_database()}.{get_schema()}.CHUNKY_RENDER",
    )


def get_pat(cli_pat: Optional[str] = None) -> Optional[str]:
    """Programmatic Access Token for the SQL API. Never written to config.json."""
    return _resolve(cli_pat, ("CHUNKY_SF_PAT", "SNOWFLAKE_PAT"), "__never__", None)


def get_private_key_path(cli_path: Optional[str] = None) -> Optional[str]:
    return _resolve(
        cli_path, ("CHUNKY_SF_PRIVATE_KEY",), "private_key_path", None
    )


def describe_config() -> dict:
    """Every effective value plus which tier produced it.

    A value alone does not tell you whether editing config.json will change
    anything -- an env var or a stale ~/.snowball entry may be shadowing it.
    """
    out = {}
    account, src = _resolve_with_source(
        None, ("CHUNKY_SF_ACCOUNT", "SNOWFLAKE_ACCOUNT", "SNOWBALL_ACCOUNT"),
        "account", ACCOUNT, from_snowball=True)
    out["account"] = f"{account} ({src})"

    user, src = _resolve_with_source(
        None, ("CHUNKY_SF_USER", "SNOWFLAKE_USER", "SNOWBALL_USER"),
        "user", None, from_snowball=True)
    out["user"] = f"{user} ({src})" if user else "(not set) — required"

    for label, env, key, default in (
        ("role", ("CHUNKY_SF_ROLE", "SNOWFLAKE_ROLE"), "role", DEFAULT_ROLE),
        ("warehouse", ("CHUNKY_SF_WAREHOUSE", "SNOWFLAKE_WAREHOUSE"), "warehouse", DEFAULT_WAREHOUSE),
        ("database", ("CHUNKY_SF_DATABASE",), "database", DEFAULT_DATABASE),
        ("schema", ("CHUNKY_SF_SCHEMA",), "schema", DEFAULT_SCHEMA),
        ("lib_stage", ("CHUNKY_SF_LIB_STAGE",), "lib_stage", None),
        ("docs_stage", ("CHUNKY_SF_DOCS_STAGE",), "docs_stage", None),
        ("render_stage", ("CHUNKY_SF_RENDER_STAGE",), "render_stage", None),
    ):
        value, src = _resolve_with_source(None, env, key, default)
        if value is None and key.endswith("_stage"):
            value, src = {
                "lib_stage": get_lib_stage(),
                "docs_stage": get_docs_stage(),
                "render_stage": get_render_stage(),
            }[key], "derived from database/schema"
        out[label] = f"{value} ({src})"

    out["config_file"] = f"{CONFIG_PATH} ({'exists' if CONFIG_PATH.exists() else 'not created'})"
    out["snowball_config"] = (
        f"{SNOWBALL_CONFIG_PATH} "
        f"({'readable — used for user/account fallback' if SNOWBALL_CONFIG_PATH.is_file() else 'absent'})"
    )
    return out
