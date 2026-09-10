"""End-to-end inspection checks for strict, content-redacted CSV diagnostics."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ingest_sentry import inspect_file, inspection


def codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def test_clean_csv_reports_metadata_and_strict_read_suggestion(make_file):
    path = make_file("id,name\n1,Ada\n2,Lin\n")
    original = path.read_bytes()
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.ok and report.complete
    assert report.records == 2
    assert report.columns == 2
    assert report.physical_lines == 3
    assert report.line_endings == {"LF": 3, "CRLF": 0, "CR": 0}
    assert report.byte_count == len(original)
    assert report.sha256 == hashlib.sha256(original).hexdigest()
    assert report.source == "input.csv"
    assert report.schema_version == "1"
    assert report.detector_version
    assert report.encoding.origin == "provided"
    assert report.encoding.complete
    assert report.encoding.bytes_validated == len(original)
    assert report.issue_count == report.error_count == report.warning_count == 0
    assert report.suggested_read_csv_kwargs == {
        "encoding": report.encoding.name,
        "encoding_errors": "strict",
        "sep": ",",
        "header": 0,
        "quotechar": '"',
        "doublequote": True,
    }
    assert path.read_bytes() == original


def test_json_report_is_serializable_and_redacts_paths_and_contents(make_file):
    path = make_file(
        "secret_account_number,name\n987654321,VERY_PRIVATE_CLIENT\n",
        name="private-client-folder/export.csv",
    )
    report = inspect_file(path, encoding="utf-8", delimiter=",")
    payload = json.dumps(report.to_dict())

    assert report.source == "export.csv"
    assert str(path) not in payload
    assert "private-client-folder" not in payload
    assert "secret_account_number" not in payload
    assert "987654321" not in payload
    assert "VERY_PRIVATE_CLIENT" not in payload
    assert json.loads(payload)["ok"] is True


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_infers_supported_delimiters(make_file, delimiter):
    path = make_file(f"id{delimiter}name\n1{delimiter}Ada\n2{delimiter}Lin\n")
    report = inspect_file(path, encoding="utf-8")

    assert report.ok
    assert report.dialect is not None
    assert report.dialect.delimiter == delimiter
    assert report.dialect.inferred
    assert report.columns == 2
    assert report.records == 2


def test_tsv_extension_defaults_to_tab(make_file):
    path = make_file("id\tname\n1\tAda\n", name="export.tsv")
    report = inspect_file(path, encoding="utf-8")

    assert report.ok
    assert report.format == "tsv"
    assert report.dialect.delimiter == "\t"


def test_custom_delimiter_is_authoritative(make_file):
    path = make_file("id:name\n1:Ada\n")
    report = inspect_file(path, encoding="utf-8", delimiter=":")

    assert report.ok
    assert report.columns == 2
    assert report.dialect.delimiter == ":"
    assert not report.dialect.inferred


def test_no_delimiter_evidence_allows_single_column(make_file):
    report = inspect_file(make_file("name\nAda\nLin\n"), encoding="utf-8")

    assert report.ok
    assert report.columns == 1
    assert report.records == 2


def test_equal_delimiter_candidates_are_not_silently_chosen(make_file):
    path = make_file("a,b;c\n1,2;3\n4,5;6\n")
    report = inspect_file(path, encoding="utf-8")

    assert not report.ok
    assert "dialect_ambiguous" in codes(report)
    assert report.suggested_read_csv_kwargs is None


def test_explicit_delimiter_resolves_ambiguity(make_file):
    path = make_file("a,b;c\n1,2;3\n4,5;6\n")
    report = inspect_file(path, encoding="utf-8", delimiter=";")

    assert report.ok
    assert report.columns == 2
    assert "dialect_ambiguous" not in codes(report)


def test_quoted_delimiters_do_not_inflate_column_count(make_file):
    path = make_file('id,note\n1,"a,b,c;d;e"\n2,"x,y,z;p;q"\n')
    report = inspect_file(path, encoding="utf-8")

    assert report.ok
    assert report.dialect.delimiter == ","
    assert report.columns == 2


def test_no_header_uses_first_data_record_width(make_file):
    path = make_file("1,Ada\n2,Lin\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",", header=False)

    assert report.ok
    assert report.records == 2
    assert report.columns == 2
    assert not report.dialect.header
    assert report.suggested_read_csv_kwargs["header"] is None


def test_multiline_quotes_and_blank_lines_preserve_physical_locations(make_file):
    path = make_file('id,note\r\n1,"hello\r\nworld"\r\n\r\n2,done\r\n3\r\n')
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.complete and not report.ok
    assert report.records == 3
    assert report.physical_lines == 6
    assert report.line_endings == {"LF": 0, "CRLF": 6, "CR": 0}
    issue = next(issue for issue in report.issues if issue.code == "column_count")
    assert issue.record == 4
    assert issue.line_start == issue.line_end == 6
    assert report.suggested_read_csv_kwargs is None


def test_multiline_ragged_record_has_entire_line_span(make_file):
    path = make_file('id,note\n1,"two\nlines",extra\n')
    report = inspect_file(path, encoding="utf-8", delimiter=",")
    issue = next(issue for issue in report.issues if issue.code == "column_count")

    assert issue.record == 2
    assert issue.line_start == 2
    assert issue.line_end == 3


def test_empty_physical_lines_are_not_data_records(make_file):
    path = make_file("\nid,name\n\n1,Ada\n\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.ok
    assert report.records == 1
    assert report.physical_lines == 5


def test_quoted_empty_field_is_a_record_not_a_blank_line(make_file):
    path = make_file('name\n""\nAda\n')
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.ok
    assert report.records == 2


@pytest.mark.parametrize("ending", ["", "\n", "\r\n", "\r"])
def test_line_endings_and_missing_final_newline(make_file, ending):
    path = make_file("id,name\n1,Ada" + ending)
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.ok
    assert report.physical_lines == 2
    assert report.records == 1
    assert report.line_endings == {
        "LF": 1 + int(ending == "\n"),
        "CRLF": int(ending == "\r\n"),
        "CR": int(ending == "\r"),
    }


@pytest.mark.parametrize("body", ['id,note\n1,"unterminated\n', 'id,note\n1,"closed"garbage\n'])
def test_malformed_quotes_are_parse_errors(make_file, body):
    report = inspect_file(make_file(body), encoding="utf-8", delimiter=",")

    assert not report.ok
    assert not report.complete
    assert "csv_parse_error" in codes(report)
    assert report.suggested_read_csv_kwargs is None


def test_doubled_quotes_are_valid(make_file):
    path = make_file('id,note\n1,"say ""hello"""\n')
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.ok
    assert report.records == 1


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x07", "\x1b"])
def test_controls_are_reported_without_cell_contents(make_file, control):
    path = make_file(f"id,note\n1,VERY_PRIVATE{control}CLIENT\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert not report.ok
    assert "control_character" in codes(report)
    assert "VERY_PRIVATE" not in json.dumps(report.to_dict())


def test_blank_and_duplicate_headers_are_warnings_only(make_file):
    path = make_file("private_name,private_name,\n1,2,3\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert report.ok
    assert {"blank_header", "duplicate_header"} <= codes(report)
    assert report.warning_count == 2
    assert report.error_count == 0
    assert "private_name" not in json.dumps(report.to_dict())


def test_no_header_does_not_issue_header_warnings(make_file):
    path = make_file("same,same,\n1,2,3\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",", header=False)

    assert report.ok
    assert not {"blank_header", "duplicate_header"} & codes(report)


def test_issue_retention_limit_does_not_hide_total_error_count(make_file):
    path = make_file("a,b\n" + "missing\n" * 7)
    report = inspect_file(path, encoding="utf-8", delimiter=",", max_issues=2)

    assert report.complete and not report.ok
    assert len(report.issues) == 2
    assert report.issues_truncated
    assert report.issue_count == report.error_count == 7
    assert report.warning_count == 0
    assert report.records == 7


def test_issue_retention_at_exact_limit_is_not_truncated(make_file):
    path = make_file("a,b\nmissing\nmissing\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",", max_issues=2)

    assert len(report.issues) == 2
    assert report.issue_count == 2
    assert not report.issues_truncated


def test_ragged_row_beyond_inference_sample_is_still_checked(make_file):
    path = make_file("id,name\n" + "1,Ada\n" * 110 + "2\n")
    report = inspect_file(path, encoding="utf-8")

    assert report.complete and not report.ok
    assert report.records == 111
    issue = next(issue for issue in report.issues if issue.code == "column_count")
    assert issue.record == issue.line_start == issue.line_end == 112


@pytest.mark.parametrize("content", [b"", b"\n\r\n"])
def test_empty_or_blank_only_file_has_no_pandas_suggestion(make_file, content):
    report = inspect_file(make_file(content), encoding="utf-8", delimiter=",")

    assert not report.ok
    assert "empty_file" in codes(report)
    assert report.records == 0
    assert report.suggested_read_csv_kwargs is None


def test_header_only_is_a_warning(make_file):
    report = inspect_file(make_file("id,name\n"), encoding="utf-8", delimiter=",")

    assert report.ok
    assert report.records == 0
    assert "no_data_records" in codes(report)
    assert report.warning_count == 1


def test_supplied_encoding_validates_invalid_tail_without_fallback(make_file):
    raw = b"id,name\n" + b"1,Ada\n" * 20_000 + b"2,\xff\n"
    report = inspect_file(make_file(raw), encoding="utf-8", delimiter=",")

    assert not report.ok
    assert not report.complete
    assert report.encoding.origin == "provided"
    assert "decode_error" in codes(report)
    assert report.suggested_read_csv_kwargs is None


def test_valid_bytes_do_not_claim_supplied_encoding_is_correct(make_file):
    path = make_file("id,name\n1,Çağrı\n", encoding="cp1254")
    report = inspect_file(path, encoding="latin-1", delimiter=",")

    # Single-byte codecs may decode successfully without matching author intent.
    assert report.encoding.origin == "provided"
    assert report.encoding.complete
    assert report.encoding.confidence is None


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_bom_unicode_is_detected_and_fully_validated(make_file, encoding):
    path = make_file("id,name\n1,Çağrı\n2,İpek\n", encoding=encoding)
    report = inspect_file(path, delimiter=",")

    assert report.ok
    assert report.encoding.origin == "detected"
    assert report.encoding.complete
    assert report.encoding.bytes_validated == path.stat().st_size
    assert report.records == 2
    assert report.columns == 2


def test_utf8_bom_does_not_hide_blank_header(make_file):
    path = make_file(",name\n1,Ada\n", encoding="utf-8-sig")
    report = inspect_file(path, delimiter=",")

    assert report.ok
    assert "blank_header" in codes(report)


def test_cp1254_supplied_encoding(make_file):
    text = "id,name\n1,Çağrı\n2,İpek\n3,Özgür\n"
    path = make_file(text, encoding="cp1254")
    report = inspect_file(path, encoding="cp1254", delimiter=",")

    assert report.ok
    assert path.read_bytes().decode(report.encoding.name) == text
    assert report.records == 3


def test_cp1254_detected_encoding_recovers_same_text(make_file):
    text = "id,note\n" + "1,İstanbul güzel bir şehir; Çağrı ışığı gördü\n" * 15
    path = make_file(text, encoding="cp1254")
    report = inspect_file(path, delimiter=",")

    assert report.ok
    assert report.encoding.origin == "detected"
    assert path.read_bytes().decode(report.encoding.name) == text


def test_detected_ambiguity_remains_a_warning_not_a_decode_failure(make_file, monkeypatch):
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
    report = inspect_file(path, delimiter=",")

    assert report.ok
    assert report.encoding.complete
    assert report.warning_count == 1
    assert report.error_count == 0
    assert "encoding_ambiguous" in codes(report)


@pytest.mark.parametrize(
    "encoding,status,validated,complete",
    [
        (None, "unknown", 0, False),
        (None, "binary", 0, True),
        ("utf-8", "text", 14, False),
        ("utf-8", "text", 1, True),
    ],
)
def test_unvalidated_detector_outcomes_never_produce_read_suggestions(
    make_file, monkeypatch, encoding, status, validated, complete
):
    path = make_file("id,name\n1,Ada\n")
    monkeypatch.setattr(
        inspection.bytesense,
        "from_fp",
        lambda snapshot: SimpleNamespace(
            encoding=encoding,
            status=status,
            confidence=0.5,
            bytes_validated=validated,
            complete=complete,
        ),
    )
    report = inspect_file(path, delimiter=",")

    assert not report.ok
    assert not report.complete
    assert not report.encoding.complete
    assert report.suggested_read_csv_kwargs is None
    assert report.error_count == 1


def test_capture_is_shared_by_detection_checksum_and_parsing(make_file, monkeypatch):
    original = b"id,name\n1,Ada\n"
    path = make_file(original)
    actual_detector = inspection.bytesense.from_fp
    calls = 0

    def mutate_original_after_capture(snapshot):
        nonlocal calls
        calls += 1
        path.write_bytes(b"changed\noriginal\nfile\n")
        return actual_detector(snapshot)

    monkeypatch.setattr(inspection.bytesense, "from_fp", mutate_original_after_capture)
    report = inspect_file(path, delimiter=",")

    assert calls == 1
    assert report.ok
    assert report.columns == 2
    assert report.records == 1
    assert report.byte_count == len(original)
    assert report.encoding.bytes_validated == len(original)
    assert report.sha256 == hashlib.sha256(original).hexdigest()


def test_authoritative_encoding_does_not_call_detector(make_file, monkeypatch):
    def unexpected_detection(snapshot):
        raise AssertionError("Supplied codec must not be replaced by detection.")

    monkeypatch.setattr(inspection.bytesense, "from_fp", unexpected_detection)
    report = inspect_file(make_file("id,name\n1,Ada\n"), encoding="utf-8", delimiter=",")

    assert report.ok


def test_binary_input_is_not_given_read_suggestions(make_file):
    path = make_file(bytes(range(256)) * 8)
    report = inspect_file(path)

    assert not report.ok
    assert not report.complete
    assert report.suggested_read_csv_kwargs is None


def test_byte_limit_exceeded_returns_incomplete_diagnostic(make_file):
    path = make_file("id,name\n1,Ada\n")
    original = path.read_bytes()
    report = inspect_file(path, encoding="utf-8", max_bytes=len(original) - 1)

    assert not report.ok
    assert not report.complete
    assert not report.encoding.complete
    assert "file_too_large" in codes(report)
    assert report.suggested_read_csv_kwargs is None
    assert path.read_bytes() == original


def test_byte_limit_exactly_equal_to_file_size_is_allowed(make_file):
    path = make_file("id,name\n1,Ada\n")
    report = inspect_file(path, encoding="utf-8", max_bytes=path.stat().st_size)

    assert report.ok
    assert report.byte_count == path.stat().st_size


def test_field_size_limit_is_a_diagnostic_and_not_a_global_mutation(make_file):
    old_limit = csv.field_size_limit()
    path = make_file("id,note\n1," + "a" * (old_limit + 1) + "\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert not report.ok
    assert not report.complete
    assert "csv_parse_error" in codes(report)
    assert csv.field_size_limit() == old_limit


def test_physical_line_bound_returns_a_diagnostic(make_file):
    path = make_file("id,note\n1," + "a" * (1024 * 1024) + "\n")
    report = inspect_file(path, encoding="utf-8", delimiter=",")

    assert not report.ok
    assert not report.complete
    assert "line_too_long" in codes(report)
    assert report.suggested_read_csv_kwargs is None


@pytest.mark.parametrize("option", ["max_bytes", "max_issues"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "100", None])
def test_limits_require_strict_positive_integers(make_file, option, value):
    path = make_file("id,name\n1,Ada\n")
    with pytest.raises(ValueError):
        inspect_file(path, **{option: value})


@pytest.mark.parametrize("delimiter", ["", ",;", "\n", "\r", '"', "\x00", 1])
def test_invalid_delimiter_is_rejected(make_file, delimiter):
    path = make_file("id,name\n1,Ada\n")
    with pytest.raises(ValueError):
        inspect_file(path, delimiter=delimiter)


@pytest.mark.parametrize(
    "encoding", ["no-such-codec", "base64_codec", "hex_codec", "rot_13", "", "   ", 42]
)
def test_non_text_or_unknown_encoding_is_rejected(make_file, encoding):
    with pytest.raises(ValueError):
        inspect_file(make_file("id,name\n1,Ada\n"), encoding=encoding)


@pytest.mark.parametrize("header", [0, 1, "yes"])
def test_header_requires_a_boolean(make_file, header):
    with pytest.raises(ValueError):
        inspect_file(make_file("id,name\n1,Ada\n"), header=header)


def test_missing_path_propagates_os_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        inspect_file(tmp_path / "missing.csv")


def test_directory_propagates_os_error(tmp_path: Path):
    with pytest.raises(OSError):
        inspect_file(tmp_path)


def test_string_paths_are_accepted(make_file):
    path = make_file("id,name\n1,Ada\n")
    assert inspect_file(str(path), encoding="utf-8", delimiter=",").ok
