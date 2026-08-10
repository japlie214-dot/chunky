"""chunky/procedure/deploy — the local toolchain that applies this repo to Snowflake.

This is a deliberately small copy of `snowball`'s auth layer with the
read-only guard removed. snowball is the right tool for exploring Snowflake
and the wrong tool for building it: `sbcore/sqlguard.py` rejects every DDL and
DML statement by design. Chunky needs to CREATE PROCEDURE, PUT bundles, CALL
procedures that INSERT, and DROP test objects, so the guard is gone -- but the
authentication mechanism, which is the part that was actually hard to get
right on Windows, is reused verbatim.

Modules
-------
config      -- configuration resolution (flag > env > config.json > ~/.snowball > default)
winkeyring  -- chunked Windows Credential Manager backend (vendored from snowball)
auth        -- OAuth authorization-code connect, token cache introspection
sqlsplit    -- $$-aware SQL script splitter (procedure bodies break naive splitters)
sfapi       -- Snowflake SQL API v2 client with 202/poll handling
sf          -- the CLI
"""

__version__ = "0.1.0"
