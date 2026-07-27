"""
procedure/utils — shared Python modules for the Chunky Snowflake procedures.

This package is bundled into `procedure/utils_bundle.zip` by
`procedure/build_procedures.py` and uploaded to `@<DB>.<SCHEMA>.STG_LIB`.
Every stored procedure then IMPORTS that zip so the handlers can do:

    from utils.init_table import run as init_table
    from utils.grant_table import run as grant_table
    ...

Modules
-------
constants         — shared configuration values (DB/schema/model/etc.)
query_log         — QueryLog: collects query_ids + pre/post timestamps
init_table        — chunky_internal_init_table handler
grant_table       — chunky_internal_grant_table handler
surgical_delete   — chunky_internal_surgical_delete handler
parse_pdf         — chunky_internal_parse_pdf handler
build_chunk_ref   — chunky_internal_build_chunk_ref handler
page_mapping      — RangeMapping / RangeMappingEngine (surgical math)
metadata_handler  — chunk metadata stamping
revert            — TIME TRAVEL-based revert helpers
"""
