import json
import sys
import zipfile
import io
import types
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def test_ulid_shape_and_sortability():
    from utils.ulid import is_ulid, new_ulid, chunk_id, run_id
    assert is_ulid(new_ulid())
    assert chunk_id().startswith("CHK_")
    assert run_id().startswith("RUN_")

def test_identifier_and_required_validation():
    from utils._shared import safe_identifier, safe_stage_path, require
    assert safe_identifier("SMOKE_CHUNKS") == "SMOKE_CHUNKS"
    assert safe_stage_path("@SBOX_DB.AI_SB.DOCS/x")
    try:
        require({"db": "SBOX_DB"}, "db", "schema")
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("missing schema was accepted")

def test_registry_help_and_unknown_command_recovery():
    from utils.chunky_ingest_handler import run
    help_result = run(None, "help", {})
    assert help_result["success"]
    assert "ingest" in {item["command"] for item in help_result["data"]["commands"]}
    failure = run(None, "ingets", {})
    assert not failure["success"]
    assert "Did you mean 'ingest'?" in failure["remedy"]

def test_all_registered_commands_declare_fields():
    """T1 guard: dispatch must never reject every field by empty metadata."""
    from utils.chunky_ingest_handler import COMMANDS as ingest_commands
    from utils.chunky_qa_handler import COMMANDS as qa_commands
    from utils.chunky_deploy_handler import COMMANDS as deploy_commands
    for procedure, commands in (("ingest", ingest_commands),
                                ("qa", qa_commands),
                                ("deploy", deploy_commands)):
        empty = [name for name, spec in commands.items() if not spec.get("fields")]
        assert not empty, f"{procedure} commands missing field metadata: {empty}"
    assert "run_id" in ingest_commands["ingest"]["fields"]

def test_bundle_has_both_poppler_architectures():
    bundles = sorted((ROOT / "build" / "out").glob("utils_bundle_*.zip"))
    assert bundles, "build the bundle before running this test"
    with zipfile.ZipFile(bundles[-1]) as archive:
        names = set(archive.namelist())
    assert "pdf2image/__init__.py" in names
    for arch in ("arm64", "x86_64"):
        assert f"poppler_bundle/{arch}/poppler/bin/pdftoppm" in names

def test_comment_payload_round_trip():
    from utils.table_comment import KEY, SCHEMA_VERSION
    payload = {KEY: {"schema_version": SCHEMA_VERSION, "sources": [{"pdf_name": "a.pdf"}]}}
    assert json.loads(json.dumps(payload))[KEY]["schema_version"] == 2

def test_lease_writes_use_explicit_scoped_transactions():
    """Lease mutations must commit inside the procedure for other sessions."""
    from utils import locks

    class Log:
        def __init__(self): self.sql = []
        def execute(self, statement, params=None):
            self.sql.append(statement)
            return []

    log = Log()
    original = locks._write_slot
    locks._write_slot = lambda *args: log.sql.append("WRITE_SLOT")
    try:
        locks._write_slot_committed(None, log, "DB", "SC", "T", "ingest", {})
    finally:
        locks._write_slot = original

    assert log.sql == ["BEGIN", "WRITE_SLOT", "COMMIT"]

def test_deploy_builder_common_tail_and_source_modes():
    from utils.chunky_deploy_handler import _build_create_ddl

    class Row(dict):
        def as_dict(self): return dict(self)

    class Log:
        def __init__(self): self.sql = []
        def execute(self, statement, params=None):
            self.sql.append(statement)
            if "INFORMATION_SCHEMA.COLUMNS" in statement:
                return [Row(COLUMN_NAME=x) for x in
                        ("CHUNK_ID", "PDF_NAME", "PAGE_NUMBER", "CHUNK",
                         "CHUNK_METADATA", "PAGE_SCREENSHOT")]
            return [Row(W="WH_XS")]

    for columns in (1, 2, 3):
        log = Log()
        search = ["CHUNK"] + (["PDF_NAME"] if columns > 1 else [])
        if columns > 2:
            search.append("PAGE_NUMBER")
        ddl, _ = _build_create_ddl(
            None, log,
            {"db": "DB", "schema": "SC", "service_name": "SVC",
             "tables": ["T"], "search_columns": search},
            "RUN_TEST",
        )
        assert 'WAREHOUSE = "WH_XS"' in ddl
        assert "TARGET_LAG" in ddl
        assert "PAGE_SCREENSHOT" not in ddl
        assert "UNION ALL" not in ddl
        if columns == 1:
            assert ' ON "CHUNK"' in ddl

    log = Log()
    ddl, meta = _build_create_ddl(
        None, log,
        {"db": "DB", "schema": "SC", "service_name": "SVC",
         "tables": ["T", "U"], "search_columns": ["CHUNK"]},
        "RUN_TEST",
    )
    assert "UNION ALL" in ddl
    assert meta["combine"] == "union"

def test_join_predicate_qualifies_both_source_aliases():
    from utils.chunky_deploy_handler import _source_query
    query, mode = _source_query(
        None, None, "DB", "SC", ["SMOKE_CHUNKS", "UNIT_CHUNKS"],
        [{"column": "CHUNK", "table": "SMOKE_CHUNKS", "expression": '"CHUNK"'}],
        [{"column": "PDF_NAME", "table": "SMOKE_CHUNKS", "expression": '"PDF_NAME"'},
         {"column": "PDF_NAME", "table": "UNIT_CHUNKS", "expression": '"PDF_NAME"'}],
        {"combine": "join", "join_type": "INNER",
         "join_on": [{"left": "SMOKE_CHUNKS.PDF_NAME", "right": "UNIT_CHUNKS.PDF_NAME"}]},
    )
    assert mode == "join"
    assert '"SMOKE_CHUNKS"."PDF_NAME" = "UNIT_CHUNKS"."PDF_NAME"' in query

def test_ingest_emits_six_column_sql_and_ulid_screenshot(monkeypatch):
    """Exercise run(), then inspect the SQL actually sent to the mock session."""
    from utils import chunky_ingest_handler as handler

    class Result:
        def __init__(self, rows=None): self.rows = rows or []
        def collect(self): return self.rows

    class Files:
        def get_stream(self, _path):
            return io.BytesIO((ROOT / "script" / "pdf" /
                               "fy2024-tbk-investor-presentation.pdf").read_bytes())
        def put(self, *args, **kwargs): return None

    class Session:
        def __init__(self): self.sql_text = []; self.file = Files()
        def sql(self, text, params=None):
            self.sql_text.append(text)
            if "CURRENT_TIMESTAMP" in text: return Result([{"TS": "2026-01-01"}])
            if "LAST_QUERY_ID" in text: return Result([{"QID": "Q1"}])
            if "AI_PARSE_DOCUMENT" in text:
                return Result([{"J": {"pages": [{"index": 0, "content": "hello"}]}}])
            if "CURRENT_USER" in text: return Result([{"U": "TESTER"}])
            return Result([])
        def write_pandas(self, *args, **kwargs): return None

    session = Session()
    monkeypatch.setitem(sys.modules, "pandas", types.SimpleNamespace(
        DataFrame=lambda rows: rows))
    monkeypatch.setattr(handler, "_page_screenshot_b64", lambda *args, **kwargs: "c2NyZWVu")
    result = handler.run(session, "ingest", {
        "db": "SBOX_DB", "schema": "AI_SB", "table": "UNIT_CHUNKS",
        "stage_path": "@SBOX_DB.AI_SB.DOCS", "file": "unit.pdf",
        "mode": "OVERWRITE", "layout": True, "vision": False,
    })
    assert result["success"] is True, result
    create = next(sql for sql in session.sql_text if "CREATE TABLE IF NOT EXISTS" in sql)
    insert = next(sql for sql in session.sql_text if "INSERT INTO" in sql)
    assert "CHUNK_ID VARCHAR NOT NULL" in create
    assert "PAGE_SCREENSHOT BINARY" in create
    assert "CHUNK_TYPE" not in create and "RELATIVE_PATH" not in create
    assert "CHUNK_METADATA" in insert and "PAGE_SCREENSHOT" in insert
    assert "CHK_" in insert and "RANDSTR(16" in insert


def test_link_annotations_include_external_and_internal_targets():
    from utils.chunky_ingest_handler import extract_link_details, format_link_details
    pdf = (ROOT / "script" / "pdf" / "chunky_link_test.pdf").read_bytes()
    external = extract_link_details(pdf, 1)
    internal = extract_link_details(pdf, 4)
    assert external == [{"type": "external", "target": "https://example.com/investor-relations"}]
    assert internal and internal[0]["type"] == "internal"
    assert "[External links:" in format_link_details(external)
    assert "[Internal links:" in format_link_details(internal)


def test_qa_help_distinguishes_literal_search_and_requires_inputs():
    from utils.chunky_qa_handler import COMMANDS
    assert "literal substring" in COMMANDS["search"]["summary"]
    assert COMMANDS["inspect"]["fields"]["chunk_id"]["required"]
    assert COMMANDS["generate_draft"]["fields"]["stage_path"]["required"]


def test_warm_serving_service_is_ready_without_index_success(monkeypatch):
    from utils.chunky_deploy_handler import _wait_ready

    class Log:
        def execute(self, statement, params=None):
            return [{"INDEXING_STATE": "REBUILDING", "SERVING_STATE": "ACTIVE"}]

    ready, data = _wait_ready(None, Log(), "DB", "SC", "SVC", timeout=1, poll=1)
    assert ready is True
    assert data["warm"] is True
