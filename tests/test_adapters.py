"""Optional DataFrame imports preserve inspected records and never infer values."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
import traceback
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ingest_sentry import adapters, inspection
from ingest_sentry.contracts import ColumnRule, ImportContract
from ingest_sentry.errors import InspectionFailed


@pytest.fixture(params=["pandas", "polars"])
def engine(request):
    return request.param


@pytest.fixture
def reader(engine):
    return getattr(adapters, f"read_{engine}")


@pytest.fixture
def real_backend(engine):
    return pytest.importorskip(engine)


def _records(frame, engine):
    if engine == "pandas":
        return frame.to_dict(orient="records")
    return frame.to_dicts()


def _fake_backend(monkeypatch, construct):
    original = importlib.import_module

    def load(name, package=None):
        if name in {"pandas", "polars"}:
            return SimpleNamespace(DataFrame=construct, String="string")
        return original(name, package)

    monkeypatch.setattr(adapters.importlib, "import_module", load)


def test_module_import_does_not_import_optional_backends():
    program = """
import sys

class RejectOptionalImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pandas', 'polars'}:
            raise AssertionError('Optional import during core initialization')

sys.meta_path.insert(0, RejectOptionalImports())
import ingest_sentry.adapters
assert 'pandas' not in sys.modules
assert 'polars' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_missing_optional_dependency_has_install_instructions(
    reader, engine, make_file, monkeypatch
):
    original = importlib.import_module

    def missing(name, package=None):
        if name == engine:
            raise ModuleNotFoundError(f"No module named {engine!r}")
        return original(name, package)

    monkeypatch.setattr(adapters.importlib, "import_module", missing)
    with pytest.raises(ImportError) as caught:
        reader(make_file("id,name\n1,Ada\n"), encoding="utf-8", delimiter=",")
    assert f"pip install 'ingest-sentry[{engine}]'" in str(caught.value)


@pytest.mark.parametrize("encoding", ["utf-8", "cp1254", "utf-16"])
def test_every_cell_remains_exact_text(reader, engine, real_backend, make_file, encoding):
    path = make_file(
        "id,name,empty,missing,decimal\n"
        "0001,Çağrı,,NA,12345678901234567890.123456789\n"
        '0002,İpek,"",null,-0.00000000000000000001\n',
        encoding=encoding,
    )
    original = path.read_bytes()
    frame = reader(path, encoding=encoding, delimiter=",")
    assert _records(frame, engine) == [
        {
            "id": "0001",
            "name": "Çağrı",
            "empty": "",
            "missing": "NA",
            "decimal": "12345678901234567890.123456789",
        },
        {
            "id": "0002",
            "name": "İpek",
            "empty": "",
            "missing": "null",
            "decimal": "-0.00000000000000000001",
        },
    ]
    if engine == "pandas":
        assert all(str(dtype) == "string" for dtype in frame.dtypes)
        assert not frame.isna().any().any()
    else:
        assert frame.dtypes == [real_backend.String] * 5
        assert frame.null_count().row(0) == (0, 0, 0, 0, 0)
    assert path.read_bytes() == original


def test_header_names_are_preserved_exactly(reader, engine, real_backend, make_file):
    frame = reader(make_file(" name ,Name,na.me\n1,2,3\n"), encoding="utf-8", delimiter=",")
    assert list(frame.columns) == [" name ", "Name", "na.me"]
    assert _records(frame, engine) == [{" name ": "1", "Name": "2", "na.me": "3"}]


def test_headerless_records_receive_stable_names(reader, engine, real_backend, make_file):
    frame = reader(make_file("0001,Ada\n0002,Lin\n"), encoding="utf-8", delimiter=",", header=False)
    assert list(frame.columns) == ["column_1", "column_2"]
    assert _records(frame, engine) == [
        {"column_1": "0001", "column_2": "Ada"},
        {"column_1": "0002", "column_2": "Lin"},
    ]


def test_header_only_produces_empty_string_columns(reader, real_backend, make_file):
    frame = reader(make_file("id,name\n"), encoding="utf-8", delimiter=",")
    assert list(frame.columns) == ["id", "name"]
    assert frame.shape == (0, 2)
    assert all(str(dtype).lower() == "string" for dtype in frame.dtypes)


def test_tsv_default_delimiter_and_encoding_detection(reader, engine, real_backend, make_file):
    frame = reader(make_file("id\tname\n0001\tAda\n", name="input.tsv"))
    assert _records(frame, engine) == [{"id": "0001", "name": "Ada"}]


def test_contract_checks_types_without_converting_values(reader, engine, real_backend, make_file):
    contract = ImportContract(
        (
            ColumnRule("id", "integer"),
            ColumnRule("price", "number"),
            ColumnRule("active", "boolean"),
            ColumnRule("date", "date"),
            ColumnRule("note", "string", nullable=True),
        ),
        encoding="utf-8",
        delimiter=",",
    )
    frame = reader(
        make_file("id,price,active,date,note\n0001,000.0100,true,2026-09-10,\n"),
        contract=contract,
    )
    assert _records(frame, engine) == [
        {"id": "0001", "price": "000.0100", "active": "true", "date": "2026-09-10", "note": ""}
    ]


def test_headerless_contract_validates_but_uses_positional_names(
    reader, engine, real_backend, make_file
):
    contract = ImportContract(
        (ColumnRule("id", "integer"), ColumnRule("name")),
        encoding="utf-8",
        delimiter=";",
        header=False,
    )
    frame = reader(make_file("0001;Ada\n0002;Lin\n"), contract=contract)
    assert _records(frame, engine) == [
        {"column_1": "0001", "column_2": "Ada"},
        {"column_1": "0002", "column_2": "Lin"},
    ]


def test_contract_failure_never_reaches_backend(reader, make_file, monkeypatch):
    contract = ImportContract(
        (ColumnRule("private_header", "integer"), ColumnRule("name")),
        encoding="utf-8",
        delimiter=",",
    )
    calls = []
    _fake_backend(monkeypatch, lambda *args, **kwargs: calls.append(args))
    with pytest.raises(InspectionFailed) as caught:
        reader(make_file("private_header,name\nSECRET_CELL,Ada\n"), contract=contract)
    assert calls == []
    payload = str(caught.value) + json.dumps(caught.value.report.to_dict())
    assert "SECRET_CELL" not in payload
    assert "private_header" not in payload


def test_multiline_and_blank_records_match_inspection(reader, engine, real_backend, make_file):
    frame = reader(
        make_file(' \t\r\nname,note\r\n1,"hello\r\nworld"\r\n\r\n2,""\r\n3, \r\n'),
        encoding="utf-8",
        delimiter=",",
    )
    assert _records(frame, engine) == [
        {"name": "1", "note": "hello\r\nworld"},
        {"name": "2", "note": ""},
        {"name": "3", "note": " "},
    ]


def test_quoted_empty_and_whitespace_single_column_rows_survive(
    reader, engine, real_backend, make_file
):
    frame = reader(make_file('name\n\n""\n" "\n\t\nAda\n'), encoding="utf-8", delimiter=",")
    assert _records(frame, engine) == [{"name": ""}, {"name": " "}, {"name": "Ada"}]


def test_source_mutation_after_capture_cannot_change_import(
    reader, engine, real_backend, make_file, monkeypatch
):
    original = b"id,name\n0001,Ada\n"
    path = make_file(original)
    detect = inspection.bytesense.from_fp
    calls = []

    def mutate(snapshot):
        calls.append(snapshot)
        path.write_bytes(b"untrusted\nchanged\n")
        return detect(snapshot)

    monkeypatch.setattr(inspection.bytesense, "from_fp", mutate)
    frame = reader(path, delimiter=",")
    assert len(calls) == 1
    assert _records(frame, engine) == [{"id": "0001", "name": "Ada"}]
    assert path.read_bytes() == b"untrusted\nchanged\n"


def test_snapshot_stays_owned_by_session(reader, make_file, monkeypatch):
    path = make_file("id,name\n1,Ada\n")
    session = inspection._inspection_session
    snapshots = []

    @contextmanager
    def track_snapshot(*args, **kwargs):
        with session(*args, **kwargs) as (report, snapshot):
            snapshots.append(snapshot)
            yield report, snapshot
            assert not snapshot.closed

    def construct(rows, **kwargs):
        assert not snapshots[0].closed
        return rows

    monkeypatch.setattr(inspection, "_inspection_session", track_snapshot)
    _fake_backend(monkeypatch, construct)
    assert reader(path, encoding="utf-8", delimiter=",") == [["1", "Ada"]]
    assert snapshots[0].closed


@pytest.mark.parametrize(
    "body",
    [
        "private_header,name\nSECRET_CELL\n",
        "private_header,name\nSECRET_CELL,Ada,extra\n",
        'private_header,name\nSECRET_CELL,"unterminated\n',
        "private_header,name\nSECRET_CELL,hidden\x01control\n",
        "private_header,name\nSECRET_CELL,Ada\n\x0b\n",
        "",
    ],
)
def test_invalid_input_never_reaches_backend(reader, make_file, monkeypatch, body):
    calls = []
    _fake_backend(monkeypatch, lambda *args, **kwargs: calls.append(args))
    with pytest.raises(InspectionFailed) as caught:
        reader(make_file(body), encoding="utf-8", delimiter=",")
    assert calls == []
    assert "SECRET_CELL" not in str(caught.value)
    assert "private_header" not in json.dumps(caught.value.report.to_dict())
    assert "SECRET_CELL" not in json.dumps(caught.value.report.to_dict())


@pytest.mark.parametrize("header", ["private,private", "private,", "private, \t"])
def test_bad_headers_rejected_even_when_warnings_were_truncated(
    reader, make_file, monkeypatch, header
):
    path = make_file(f"{header}\n1,2\n")
    monkeypatch.setattr(
        inspection.bytesense,
        "from_fp",
        lambda snapshot: SimpleNamespace(
            encoding="utf-8",
            status="ambiguous",
            confidence=0.5,
            bytes_validated=path.stat().st_size,
            complete=True,
        ),
    )
    calls = []
    _fake_backend(monkeypatch, lambda *args, **kwargs: calls.append(args))
    with pytest.raises(InspectionFailed, match="nonblank, unique") as caught:
        reader(path, delimiter=",", max_issues=1, accept_ambiguous=True)
    assert caught.value.report.ok
    assert caught.value.report.issues_truncated
    assert [issue.code for issue in caught.value.report.issues] == ["encoding_ambiguous"]
    assert calls == []


def test_ambiguous_detection_requires_explicit_opt_in(reader, make_file, monkeypatch):
    path = make_file("id,name\n1,Ada\n")
    monkeypatch.setattr(
        inspection.bytesense,
        "from_fp",
        lambda snapshot: SimpleNamespace(
            encoding="utf-8",
            status="ambiguous",
            confidence=0.5,
            bytes_validated=path.stat().st_size,
            complete=True,
        ),
    )
    _fake_backend(monkeypatch, lambda rows, **kwargs: rows)
    with pytest.raises(InspectionFailed, match="Ambiguous encoding"):
        reader(path, delimiter=",")
    assert reader(path, delimiter=",", accept_ambiguous=True) == [["1", "Ada"]]
    assert reader(path, encoding="utf-8", delimiter=",") == [["1", "Ada"]]


@pytest.mark.parametrize("failure", [ValueError, TypeError, RuntimeError])
def test_backend_errors_and_tracebacks_redact_cell_contents(
    reader, make_file, monkeypatch, failure
):
    path = make_file("private_header,name\nSECRET_CELL,Ada\n")

    def fail(rows, **kwargs):
        raise failure(rows[0][0])

    _fake_backend(monkeypatch, fail)
    with pytest.raises(InspectionFailed, match="could not be materialized") as caught:
        reader(path, encoding="utf-8", delimiter=",")
    assert "SECRET_CELL" not in "".join(traceback.format_exception(caught.value))
    assert "SECRET_CELL" not in json.dumps(caught.value.report.to_dict())
    assert "private_header" not in json.dumps(caught.value.report.to_dict())
    assert caught.value.__cause__ is None
    assert caught.value.report.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_process_control_exceptions_propagate(reader, make_file, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure()

    _fake_backend(monkeypatch, fail)
    with pytest.raises(failure):
        reader(make_file("id\n1\n"), encoding="utf-8", delimiter=",")


def test_input_io_errors_propagate(reader, tmp_path):
    with pytest.raises(FileNotFoundError):
        reader(tmp_path / "absent.csv", encoding="utf-8", delimiter=",")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"encoding": "not-a-real-codec"},
        {"delimiter": "bad"},
        {"header": "auto"},
        {"max_bytes": 0},
        {"max_issues": 0},
        {"accept_ambiguous": 1},
    ],
)
def test_invalid_options_propagate(reader, make_file, kwargs):
    with pytest.raises(ValueError):
        reader(make_file("id,name\n1,Ada\n"), **kwargs)


def test_backend_parser_options_cannot_bypass_inspection(reader, make_file):
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        reader(make_file("id,name\n1,Ada\n"), on_bad_lines="skip")


def test_max_bytes_rejection_preserves_report(reader, make_file):
    with pytest.raises(InspectionFailed) as caught:
        reader(make_file("id,name\n1,Ada\n"), max_bytes=8)
    assert caught.value.report.issues[0].code == "file_too_large"


def test_iteration_disagreement_is_rejected_before_backend(reader, make_file, monkeypatch):
    path = make_file("id,name\n1,Ada\n")
    monkeypatch.setattr(inspection, "_iter_rows", lambda *args: iter([["id", "name"], ["1"]]))
    calls = []
    _fake_backend(monkeypatch, lambda *args, **kwargs: calls.append(args))
    with pytest.raises(InspectionFailed, match="inconsistent column counts"):
        reader(path, encoding="utf-8", delimiter=",")
    assert calls == []


def test_no_replayed_rows_is_rejected(reader, make_file, monkeypatch):
    monkeypatch.setattr(inspection, "_iter_rows", lambda *args: iter([]))
    with pytest.raises(InspectionFailed, match="no validated tabular records"):
        reader(make_file("id,name\n1,Ada\n"), encoding="utf-8", delimiter=",")


def test_invalid_report_shape_is_rejected(reader, make_file, monkeypatch):
    original = inspection._inspection_session

    @contextmanager
    def missing_shape(*args, **kwargs):
        with original(*args, **kwargs) as (report, snapshot):
            yield replace(report, columns=None), snapshot

    monkeypatch.setattr(inspection, "_inspection_session", missing_shape)
    with pytest.raises(InspectionFailed, match="no validated tabular records"):
        reader(make_file("id,name\n1,Ada\n"), encoding="utf-8", delimiter=",")
