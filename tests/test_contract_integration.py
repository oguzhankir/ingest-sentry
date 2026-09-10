"""Import contracts apply to the complete captured input and parser configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest_sentry import inspect_file, inspection
from ingest_sentry.contracts import ColumnRule, ImportContract, load_contract


def issue_codes(report):
    return [issue.code for issue in report.issues]


def contract_from_options(tmp_path, options, *, via_json):
    if via_json:
        path = tmp_path / "import.contract.json"
        path.write_text(json.dumps(options), encoding="utf-8")
        return load_contract(path)
    return ImportContract(
        **{
            **options,
            "columns": tuple(ColumnRule(**column) for column in options["columns"]),
        }
    )


@pytest.mark.parametrize("via_json", [False, True])
def test_contract_parser_settings_are_authoritative(tmp_path, make_file, monkeypatch, via_json):
    contract = contract_from_options(
        tmp_path,
        {
            "encoding": "cp1254",
            "delimiter": ";",
            "header": False,
            "format": "csv",
            "columns": [{"name": "id", "type": "integer"}, {"name": "description"}],
        },
        via_json=via_json,
    )
    path = make_file("1;İstanbul\n2;Çağrı\n", name="partner.tsv", encoding="cp1254")
    original = path.read_bytes()

    def unexpected_detection(*args, **kwargs):
        pytest.fail("The contract's authoritative codec must bypass encoding detection.")

    monkeypatch.setattr(inspection.bytesense, "from_fp", unexpected_detection)
    report = inspect_file(path, contract=contract)
    assert report.ok and report.complete
    assert report.format == "csv"
    assert report.encoding.name == "cp1254" and report.encoding.origin == "provided"
    assert report.encoding.complete and report.encoding.bytes_validated == len(original)
    assert report.dialect.delimiter == ";" and not report.dialect.header
    assert not report.dialect.inferred
    assert report.records == 2 and report.columns == 2
    assert report.suggested_read_csv_kwargs["header"] is None
    assert path.read_bytes() == original


@pytest.mark.parametrize("via_json", [False, True])
@pytest.mark.parametrize(
    ("contract_format", "filename", "delimiter"),
    [("csv", "partner.tsv", ";"), ("tsv", "partner.csv", "\t")],
)
def test_contract_format_overrides_suffix_and_resolves_delimiter_defaults(
    tmp_path, make_file, via_json, contract_format, filename, delimiter
):
    contract = contract_from_options(
        tmp_path,
        {
            "format": contract_format,
            "encoding": "utf-8",
            "columns": [{"name": "id", "type": "integer"}, {"name": "name"}],
        },
        via_json=via_json,
    )
    path = make_file(f"id{delimiter}name\n1{delimiter}Ada\n", name=filename)
    report = inspect_file(path, contract=contract)
    assert report.ok
    assert report.format == contract_format
    assert report.dialect.delimiter == delimiter and report.dialect.header
    assert report.records == 1


def test_missing_contract_encoding_uses_detector_and_missing_delimiter_infers(make_file):
    contract = ImportContract((ColumnRule("id", "integer"), ColumnRule("name")))
    report = inspect_file(make_file("id;name\n1;Ada\n"), contract=contract)
    assert report.ok
    assert report.encoding.origin == "detected" and report.encoding.complete
    assert report.dialect.delimiter == ";" and report.dialect.inferred


@pytest.mark.parametrize("explicit_encoding", ["UTF8", "utf_8", "utf-8"])
def test_matching_explicit_options_and_codec_aliases_are_accepted(make_file, explicit_encoding):
    contract = ImportContract((ColumnRule("id", "integer"),), encoding="UTF-8", delimiter=";")
    report = inspect_file(
        make_file("id\n1\n"),
        contract=contract,
        encoding=explicit_encoding,
        delimiter=";",
        header=True,
    )
    assert report.ok and report.encoding.name == "utf-8"


@pytest.mark.parametrize(
    ("options", "conflicting_field"),
    [
        ({"encoding": "cp1254"}, "encoding"),
        ({"delimiter": ","}, "delimiter"),
        ({"header": False}, "header"),
    ],
)
def test_conflicting_explicit_options_are_rejected_before_file_access(
    tmp_path, options, conflicting_field
):
    contract = ImportContract((ColumnRule("id"),), encoding="utf-8", delimiter=";")
    with pytest.raises(ValueError, match=f"{conflicting_field} conflicts with the contract"):
        inspect_file(tmp_path / "does-not-exist.csv", contract=contract, **options)


def test_explicit_header_true_conflicts_with_headerless_contract(tmp_path):
    contract = ImportContract((ColumnRule("id"),), header=False)
    with pytest.raises(ValueError, match="header conflicts"):
        inspect_file(tmp_path / "does-not-exist.csv", contract=contract, header=True)


def test_explicit_options_fill_unspecified_contract_fields(make_file):
    contract = ImportContract((ColumnRule("id", "integer"), ColumnRule("name")))
    report = inspect_file(
        make_file("id:name\n1:İstanbul\n", encoding="cp1254"),
        contract=contract,
        encoding="cp1254",
        delimiter=":",
    )
    assert report.ok
    assert report.encoding.name == "cp1254" and report.dialect.delimiter == ":"


@pytest.mark.parametrize("invalid_contract", [{}, "contract.json", False, [], 1])
def test_invalid_contract_object_fails_before_file_access(tmp_path, invalid_contract):
    with pytest.raises(ValueError, match="contract must be an ImportContract"):
        inspect_file(tmp_path / "does-not-exist.csv", contract=invalid_contract)


def test_contract_codec_is_strict_and_does_not_fallback(make_file):
    contract = ImportContract((ColumnRule("name"),), encoding="utf-8", delimiter=";")
    report = inspect_file(make_file(b"name\nprivate-\xff\n"), contract=contract)
    assert not report.ok and not report.complete
    assert issue_codes(report) == ["decode_error"]
    assert report.encoding.name == "utf-8" and not report.encoding.complete


def test_type_null_and_header_failures_are_reported_without_contents(make_file):
    contract = ImportContract(
        (ColumnRule("SECRET_ID", "integer"), ColumnRule("SECRET_NAME")),
        encoding="utf-8",
        delimiter=",",
    )
    report = inspect_file(
        make_file("PRIVATE_ID,PRIVATE_NAME\nPRIVATE_ACCOUNT,\n2,Public\n"), contract=contract
    )
    assert report.complete and not report.ok and report.records == 2
    assert issue_codes(report) == ["contract_header", "contract_type", "contract_null"]
    assert [(issue.record, issue.line_start, issue.line_end) for issue in report.issues] == [
        (1, 1, 1),
        (2, 2, 2),
        (2, 2, 2),
    ]
    assert report.suggested_read_csv_kwargs is None
    serialized = json.dumps(report.to_dict())
    assert "SECRET" not in serialized and "PRIVATE" not in serialized


def test_wrong_header_width_is_compared_to_contract_not_just_later_rows(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"), ColumnRule("name")), encoding="utf-8", delimiter=","
    )
    report = inspect_file(make_file("id,name,extra\n1,Ada,other\n"), contract=contract)
    assert report.complete and not report.ok
    assert issue_codes(report) == ["contract_column_count", "contract_column_count"]
    assert [issue.record for issue in report.issues] == [1, 2]


def test_wrong_data_width_is_checked_by_structure_and_contract(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"), ColumnRule("name")), encoding="utf-8", delimiter=","
    )
    report = inspect_file(make_file("id,name\n1\n2,Ada\n"), contract=contract)
    assert report.complete and report.records == 2
    assert issue_codes(report) == ["contract_column_count", "column_count"]
    assert all(issue.record == 2 for issue in report.issues)


def test_headerless_first_record_is_validated_as_data(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"),), encoding="utf-8", delimiter=",", header=False
    )
    report = inspect_file(make_file("PRIVATE_VALUE\n123\n"), contract=contract)
    assert report.complete and report.records == 2
    assert issue_codes(report) == ["contract_type"]
    assert report.issues[0].record == report.issues[0].line_start == 1


def test_contract_errors_after_the_sample_are_not_missed(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"), ColumnRule("name")), encoding="utf-8", delimiter=","
    )
    text = "id,name\n" + "1,valid-name\n" * 10000 + "PRIVATE_ACCOUNT,valid\n"
    report = inspect_file(make_file(text), contract=contract)
    assert report.complete and not report.ok and report.records == 10001
    assert report.byte_count > inspection._SAMPLE_CHARS
    assert issue_codes(report) == ["contract_type"]
    assert report.issues[0].record == report.issues[0].line_start == 10002
    assert report.encoding.bytes_validated == len(text.encode())


def test_contract_issues_keep_logical_records_and_multiline_physical_spans(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"), ColumnRule("day", "date")),
        encoding="utf-8",
        delimiter=",",
    )
    report = inspect_file(
        make_file('\nid,day\n"1\n2",2023-02-29\n\n3,2024-02-29\n'), contract=contract
    )
    assert report.complete and report.records == 2 and report.physical_lines == 6
    assert issue_codes(report) == ["contract_type", "contract_type"]
    assert [(issue.record, issue.line_start, issue.line_end) for issue in report.issues] == [
        (2, 3, 4),
        (2, 3, 4),
    ]


def test_contract_issues_are_counted_after_retention_limit(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"), ColumnRule("name")), encoding="utf-8", delimiter=","
    )
    report = inspect_file(
        make_file("id,name\n" + "PRIVATE_ACCOUNT,\n" * 5), contract=contract, max_issues=3
    )
    assert report.complete and not report.ok and report.records == 5
    assert report.issue_count == report.error_count == 10 and report.warning_count == 0
    assert len(report.issues) == 3 and report.issues_truncated
    assert issue_codes(report) == ["contract_type", "contract_null", "contract_type"]
    assert "PRIVATE_ACCOUNT" not in json.dumps(report.to_dict())


@pytest.mark.parametrize(
    ("records", "expected"),
    [(0, "contract_min_records"), (1, None), (2, None), (3, "contract_max_records")],
)
def test_contract_record_bounds_count_data_only_and_are_inclusive(make_file, records, expected):
    contract = ImportContract(
        (ColumnRule("id", "integer"),),
        encoding="utf-8",
        delimiter=",",
        min_records=1,
        max_records=2,
    )
    report = inspect_file(make_file("id\n" + "1\n" * records), contract=contract)
    assert report.complete and report.records == records
    if expected is None:
        assert report.ok
    else:
        assert not report.ok and expected in issue_codes(report)
        issue = next(issue for issue in report.issues if issue.code == expected)
        assert issue.record is None and issue.line_start is None and issue.line_end is None


@pytest.mark.parametrize("bounds", [{"min_records": 10}, {"max_records": 0}])
def test_record_bounds_are_not_claimed_for_incomplete_csv(make_file, bounds):
    contract = ImportContract(
        (ColumnRule("id", "integer"),), encoding="utf-8", delimiter=",", **bounds
    )
    report = inspect_file(make_file('id\n1\n"unclosed\n'), contract=contract)
    assert not report.ok and not report.complete and report.records == 1
    assert issue_codes(report) == ["csv_parse_error"]


@pytest.mark.parametrize("failure", ["size", "decode", "line"])
def test_other_incomplete_inputs_do_not_generate_record_bound_claims(
    make_file, monkeypatch, failure
):
    contract = ImportContract(
        (ColumnRule("id", "integer"),), encoding="utf-8", delimiter=",", min_records=100
    )
    if failure == "size":
        report = inspect_file(make_file("id\n123\n"), contract=contract, max_bytes=4)
        expected = "file_too_large"
    elif failure == "decode":
        report = inspect_file(make_file(b"id\n\xff\n"), contract=contract)
        expected = "decode_error"
    else:
        monkeypatch.setattr(inspection, "_LINE_LIMIT", 10)
        report = inspect_file(make_file("id\n" + "1" * 11 + "\n"), contract=contract)
        expected = "line_too_long"
    assert not report.complete
    assert issue_codes(report) == [expected]


@pytest.mark.parametrize("content", ["", "\n \t\n"])
def test_empty_input_applies_minimum_without_accepting_missing_header(make_file, content):
    contract = ImportContract(
        (ColumnRule("id", "integer"),), encoding="utf-8", delimiter=",", min_records=1
    )
    report = inspect_file(make_file(content), contract=contract)
    assert report.complete and not report.ok and report.records == 0
    assert issue_codes(report) == ["empty_file", "contract_min_records"]


def test_header_only_can_satisfy_explicit_zero_data_contract(make_file):
    contract = ImportContract(
        (ColumnRule("id", "integer"),), encoding="utf-8", delimiter=",", max_records=0
    )
    report = inspect_file(make_file("id\n"), contract=contract)
    assert report.ok and report.complete and report.records == 0
    assert issue_codes(report) == ["no_data_records"]
    assert report.warning_count == 1 and report.error_count == 0


def test_repository_vendor_example_passes_its_contract():
    examples = Path(__file__).parents[1] / "examples"
    contract = load_contract(examples / "vendor.contract.json")
    report = inspect_file(examples / "vendor_export.csv", contract=contract)
    assert report.ok and report.complete
    assert report.records == 2 and report.columns == 3 and report.physical_lines == 4
    assert report.issue_count == 0
