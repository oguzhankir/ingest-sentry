"""Strict contract configuration, type semantics, and content-redacted diagnostics."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from ingest_sentry.contracts import (
    MAX_CONTRACT_BYTES,
    ColumnRule,
    ContractValidator,
    ImportContract,
    load_contract,
)


def load_json(tmp_path, data):
    path = tmp_path / "private.contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_contract(path)


def validation_issues(column_type, value, *, nullable=False):
    issues = []
    validator = ContractValidator(
        ImportContract((ColumnRule("private_column", column_type, nullable),)), issues.append
    )
    row = [value]
    validator.validate_row(row, 3, 4, 6)
    assert row == [value]
    for issue in issues:
        assert (issue.record, issue.line_start, issue.line_end) == (3, 4, 6)
    return issues


def test_contract_defaults_and_immutability():
    rule = ColumnRule("id")
    contract = ImportContract((rule,))
    assert rule.type == "string" and rule.nullable is False
    assert contract.header and contract.encoding is None and contract.delimiter is None
    assert contract.min_records == 0 and contract.max_records is None
    assert contract.version == 1 and contract.format == "csv"
    with pytest.raises(FrozenInstanceError):
        contract.header = False
    with pytest.raises(FrozenInstanceError):
        rule.nullable = True


@pytest.mark.parametrize("name", ["", " ", "\t\r\n", "\u2002", None, False, 1, [], {}])
def test_column_names_are_nonempty_strings(name):
    with pytest.raises(ValueError, match="Column name"):
        ColumnRule(name)


def test_nonblank_column_names_preserve_significant_spaces():
    assert ColumnRule(" id ").name == " id "


@pytest.mark.parametrize("column_type", ["STRING", "json", "float", "", None, False, [], {}])
def test_unknown_column_types_are_rejected(column_type):
    with pytest.raises(ValueError, match="Column type"):
        ColumnRule("id", column_type)


@pytest.mark.parametrize("nullable", [0, 1, "true", None, [], {}])
def test_nullable_is_a_boolean(nullable):
    with pytest.raises(ValueError, match="nullable"):
        ColumnRule("id", nullable=nullable)


@pytest.mark.parametrize(
    "columns", [(), [], [ColumnRule("id")], ("id",), (None,), (ColumnRule("id"),) * 1025]
)
def test_contract_columns_are_a_bounded_nonempty_tuple(columns):
    with pytest.raises(ValueError, match="Contract columns"):
        ImportContract(columns)


def test_duplicate_names_rejected_and_column_limit_is_inclusive():
    with pytest.raises(ValueError, match="unique"):
        ImportContract((ColumnRule("secret"), ColumnRule("secret")))
    contract = ImportContract(tuple(ColumnRule(str(index)) for index in range(1024)))
    assert len(contract.columns) == 1024


@pytest.mark.parametrize(
    ("options", "field"),
    [
        ({"version": True}, "version"),
        ({"version": 1.0}, "version"),
        ({"version": "1"}, "version"),
        ({"version": 2}, "version"),
        ({"header": 0}, "header"),
        ({"header": "false"}, "header"),
        ({"header": None}, "header"),
        ({"min_records": True}, "min_records"),
        ({"min_records": 0.0}, "min_records"),
        ({"min_records": -1}, "min_records"),
        ({"min_records": None}, "min_records"),
        ({"max_records": False}, "max_records"),
        ({"max_records": -1}, "max_records"),
        ({"max_records": 1.0}, "max_records"),
        ({"min_records": 3, "max_records": 2}, "max_records"),
        ({"format": "jsonl"}, "format"),
        ({"format": None}, "format"),
        ({"format": {}}, "format"),
    ],
)
def test_contract_options_validate_programmatic_and_json_constructors(tmp_path, options, field):
    with pytest.raises(ValueError, match=field):
        ImportContract((ColumnRule("id"),), **options)
    with pytest.raises(ValueError, match=field):
        load_json(tmp_path, {"columns": [{"name": "id"}], **options})


@pytest.mark.parametrize("delimiter", ["", ";;", '"', "\n", "\r", "\0", "\x1b", 1, False, []])
def test_invalid_delimiters_rejected(delimiter):
    with pytest.raises(ValueError, match="delimiter"):
        ImportContract((ColumnRule("id"),), delimiter=delimiter)


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|", ":", " ", "é"])
def test_printable_single_delimiters_and_tab_accepted(delimiter):
    assert ImportContract((ColumnRule("id"),), delimiter=delimiter).delimiter == delimiter


@pytest.mark.parametrize("encoding", ["UTF8", "utf_8", "UTF-8"])
def test_encoding_is_canonicalized(encoding):
    assert ImportContract((ColumnRule("id"),), encoding=encoding).encoding == "utf-8"


@pytest.mark.parametrize(
    "encoding",
    ["", " ", "PRIVATE_INVALID_CODEC", "base64_codec", "rot13", "hex", "undefined", 1, []],
)
def test_only_supported_text_codecs_are_accepted(encoding):
    with pytest.raises(ValueError) as caught:
        ImportContract((ColumnRule("id"),), encoding=encoding)
    assert "PRIVATE_INVALID_CODEC" not in str(caught.value)
    assert (
        caught.value.__suppress_context__ or not isinstance(encoding, str) or not encoding.strip()
    )


def test_loads_all_supported_fields_and_example(tmp_path):
    contract = load_json(
        tmp_path,
        {
            "version": 1,
            "format": "tsv",
            "encoding": "cp1254",
            "delimiter": "\t",
            "header": False,
            "min_records": 1,
            "max_records": 5,
            "columns": [{"name": "id", "type": "integer", "nullable": True}],
        },
    )
    assert contract == ImportContract(
        (ColumnRule("id", "integer", True),), "cp1254", "\t", False, 1, 5, "tsv"
    )
    example = load_contract(Path(__file__).parents[1] / "examples/vendor.contract.json")
    assert [column.name for column in example.columns] == ["id", "description", "amount"]
    assert example.encoding == "utf-8" and example.delimiter == ";"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "PRIVATE_VALUE",
        {},
        {"columns": []},
        {"columns": {}},
        {"columns": ["PRIVATE_VALUE"]},
        {"columns": [{}]},
        {"columns": [{"name": "id", "PRIVATE_KEY": "PRIVATE_VALUE"}]},
        {"columns": [{"name": "id"}], "PRIVATE_KEY": "PRIVATE_VALUE"},
        {"columns": [{"name": "id"}], "$ref": "https://private.invalid/schema.json"},
        {"columns": [{"name": "id"}], "include": "private.csv"},
        {"columns": [{"name": "id"}, {"name": "id"}]},
        {"columns": [{"name": str(index)} for index in range(1025)]},
    ],
)
def test_invalid_shape_unknown_keys_and_external_refs_fail_without_values(tmp_path, data):
    with pytest.raises(ValueError) as caught:
        load_json(tmp_path, data)
    text = str(caught.value)
    assert "PRIVATE" not in text and "private" not in text and str(tmp_path) not in text


@pytest.mark.parametrize(
    "raw",
    [
        b'{"columns": [], "columns": [{"name": "id"}]}',
        b'{"columns": [{"name": "PRIVATE_VALUE", "name": "id"}]}',
        b'{"columns": [{"name": "id"}], "min_records": NaN}',
        b'{"columns": [{"name": "id"}], "max_records": Infinity}',
        b'{"columns": [{"name": "id"}], "max_records": -Infinity}',
        b'{"PRIVATE_VALUE":',
        b'\xff{"columns": []}',
        b'\xef\xbb\xbf{"columns": []}',
    ],
)
def test_malformed_nonstandard_and_duplicate_json_is_redacted(tmp_path, raw):
    path = tmp_path / "PRIVATE_PATH.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="valid UTF-8 JSON") as caught:
        load_contract(path)
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_nested_non_contract_json_is_rejected(tmp_path):
    path = tmp_path / "nested.json"
    path.write_bytes(b"[" * 2000 + b"]" * 2000)
    with pytest.raises(ValueError):
        load_contract(path)


def test_json_recursion_errors_are_redacted(tmp_path, monkeypatch):
    path = tmp_path / "nested.json"
    path.write_bytes(b"{}")

    def fail(*args, **kwargs):
        raise RecursionError("PRIVATE_VALUE")

    monkeypatch.setattr("ingest_sentry.contracts.json.loads", fail)
    with pytest.raises(ValueError, match="valid UTF-8 JSON") as caught:
        load_contract(path)
    assert "PRIVATE_VALUE" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_contract_byte_limit_is_inclusive_and_counts_utf8_bytes(tmp_path):
    path = tmp_path / "limit.json"
    base = b'{"columns": [{"name": "id"}]}'
    path.write_bytes(base + b" " * (MAX_CONTRACT_BYTES - len(base)))
    assert load_contract(path).columns == (ColumnRule("id"),)
    path.write_bytes(base + b" " * (MAX_CONTRACT_BYTES + 1 - len(base)))
    with pytest.raises(ValueError, match="64 KiB"):
        load_contract(path)
    path.write_text(
        json.dumps({"columns": [{"name": "é" * 40000}]}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="64 KiB"):
        load_contract(path)


def test_filesystem_errors_preserve_oserror(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_contract(tmp_path / "missing.json")
    with pytest.raises(OSError):
        load_contract(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files require POSIX")
def test_fifo_contract_is_rejected_without_waiting_for_writer(tmp_path, monkeypatch):
    path = tmp_path / "pipe.json"
    os.mkfifo(path)
    actual_open = os.open
    opened = []

    def open_nonblocking(path, flags):
        assert flags & os.O_NONBLOCK
        fd = actual_open(path, flags)
        opened.append(fd)
        return fd

    monkeypatch.setattr("ingest_sentry.contracts.os.open", open_nonblocking)
    with pytest.raises(ValueError, match="regular file"):
        load_contract(path)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_device_contract_is_rejected():
    with pytest.raises(ValueError, match="regular file"):
        load_contract(os.devnull)


@pytest.mark.parametrize("failing_call", ["fdopen", "fstat"])
def test_contract_descriptor_closes_after_open_setup_errors(tmp_path, monkeypatch, failing_call):
    path = tmp_path / "contract.json"
    path.write_bytes(b"{}")
    actual_open = os.open
    actual_fstat = os.fstat
    opened = []

    def record_open(path, flags):
        fd = actual_open(path, flags)
        opened.append(fd)
        return fd

    def fail(*args, **kwargs):
        raise OSError("Synthetic failure")

    monkeypatch.setattr("ingest_sentry.contracts.os.open", record_open)
    monkeypatch.setattr(f"ingest_sentry.contracts.os.{failing_call}", fail)
    with pytest.raises(OSError, match="Synthetic failure"):
        load_contract(path)
    assert len(opened) == 1
    with pytest.raises(OSError):
        actual_fstat(opened[0])


def test_header_is_exact_ordered_and_content_redacted():
    issues = []
    validator = ContractValidator(
        ImportContract((ColumnRule("PRIVATE_HEADER_A"), ColumnRule("PRIVATE_HEADER_B"))),
        issues.append,
    )
    validator.validate_header(["PRIVATE_HEADER_A", "PRIVATE_HEADER_B"], 1, 1, 1)
    assert not issues
    validator.validate_header(["PRIVATE_HEADER_B", "PRIVATE_HEADER_A"], 1, 1, 2)
    assert [issue.code for issue in issues] == ["contract_header"]
    assert (issues[0].record, issues[0].line_start, issues[0].line_end) == (1, 1, 2)
    assert "PRIVATE" not in json.dumps(asdict(issues[0]))
    issues.clear()
    validator.validate_header(["PRIVATE_HEADER_A ", "PRIVATE_HEADER_B"], 1, 1, 1)
    assert issues[0].code == "contract_header"


@pytest.mark.parametrize("method", ["validate_row", "validate_header"])
@pytest.mark.parametrize("row", [[], ["id", "extra"]])
def test_width_mismatch_is_one_issue_without_partial_type_checks(method, row):
    issues = []
    validator = ContractValidator(ImportContract((ColumnRule("id", "integer"),)), issues.append)
    getattr(validator, method)(row, 7, 9, 10)
    assert len(issues) == 1 and issues[0].code == "contract_column_count"
    assert (issues[0].record, issues[0].line_start, issues[0].line_end) == (7, 9, 10)


@pytest.mark.parametrize(
    ("column_type", "values"),
    [
        ("string", [" ", "NaN", "PRIVATE_VALUE", "unicode: é中", "multi\nline"]),
        ("integer", ["0", "1", "-1", "+1", "001", "-0", "9" * 5000]),
        ("number", ["0", "-1", "+1", "1.0", ".5", "1.", "-.5", "1e3", "-1.2E-3", "1e999"]),
        ("boolean", ["true", "false"]),
        ("date", ["2024-02-29", "2025-01-31", "0001-01-01", "9999-12-31"]),
    ],
)
def test_valid_values_are_checked_without_coercion(column_type, values):
    for value in values:
        assert validation_issues(column_type, value) == []


@pytest.mark.parametrize(
    ("column_type", "values"),
    [
        ("integer", ["1.0", "1e2", " 1", "1 ", "1\n", "١", "１", "+", "--1", "1_000"]),
        (
            "number",
            [
                "NaN",
                "nan",
                "inf",
                "Infinity",
                "-inf",
                "1,000",
                "0xFF",
                " 1",
                "1\n",
                ".",
                "1e",
                "١.٢",
                "1_000",
            ],
        ),
        ("boolean", ["True", "FALSE", "0", "1", "yes", " true", "false\n"]),
        (
            "date",
            [
                "2023-02-29",
                "2024-13-01",
                "2024-04-31",
                "2024-1-01",
                "20240101",
                "2024-W01-1",
                "0000-01-01",
                "２０２４-01-01",
                "2024-01-01\n",
            ],
        ),
    ],
)
def test_invalid_values_are_not_normalized_or_replaced(column_type, values):
    for value in values:
        issues = validation_issues(column_type, value)
        assert len(issues) == 1 and issues[0].code == "contract_type"
        assert issues[0].message == "Column 1 does not match its declared type."


@pytest.mark.parametrize("column_type", ["string", "integer", "number", "boolean", "date"])
def test_empty_values_follow_nullable_for_every_type(column_type):
    assert validation_issues(column_type, "", nullable=True) == []
    issues = validation_issues(column_type, "")
    assert len(issues) == 1 and issues[0].code == "contract_null"


def test_cell_failures_use_positions_not_names_or_values():
    issues = []
    validator = ContractValidator(
        ImportContract((ColumnRule("PRIVATE_NAME", "integer"), ColumnRule("OTHER_SECRET"))),
        issues.append,
    )
    validator.validate_row(["PRIVATE_VALUE", ""], 2, 2, 3)
    assert [(issue.code, issue.message) for issue in issues] == [
        ("contract_type", "Column 1 does not match its declared type."),
        ("contract_null", "Column 2 does not permit an empty value."),
    ]
    serialized = json.dumps([asdict(issue) for issue in issues])
    assert "PRIVATE" not in serialized and "SECRET" not in serialized


@pytest.mark.parametrize(
    ("records", "expected"),
    [(0, "contract_min_records"), (1, None), (2, None), (3, "contract_max_records")],
)
def test_record_bounds_are_inclusive(records, expected):
    issues = []
    validator = ContractValidator(
        ImportContract((ColumnRule("id"),), min_records=1, max_records=2), issues.append
    )
    validator.finish(records)
    assert [issue.code for issue in issues] == ([] if expected is None else [expected])
    for issue in issues:
        assert issue.record is None and issue.line_start is None and issue.line_end is None


def test_unbounded_default_and_exact_zero_contract():
    issues = []
    ContractValidator(ImportContract((ColumnRule("id"),)), issues.append).finish(10**30)
    validator = ContractValidator(ImportContract((ColumnRule("id"),), max_records=0), issues.append)
    validator.finish(0)
    assert not issues
    validator.finish(1)
    assert issues[0].code == "contract_max_records"
