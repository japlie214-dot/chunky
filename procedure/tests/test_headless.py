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
