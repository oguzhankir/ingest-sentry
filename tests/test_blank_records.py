"""Whitespace delimiters must not make real empty-cell records disappear."""

import pytest

from ingest_sentry import adapters, inspect_file, normalize_file
from ingest_sentry.contracts import ColumnRule, ImportContract


@pytest.fixture(params=["\t", " "])
def whitespace_delimiter(request):
    return request.param


def _contract(delimiter, *, nullable=False, header=True):
    return ImportContract(
        (ColumnRule("id", nullable=nullable), ColumnRule("name", nullable=nullable)),
        encoding="utf-8",
        delimiter=delimiter,
        header=header,
    )


def test_delimiter_only_record_cannot_bypass_null_contract(make_file, whitespace_delimiter):
    delimiter = whitespace_delimiter
    path = make_file(f"id{delimiter}name\n{delimiter}\n1{delimiter}Ada\n")
    report = inspect_file(path, contract=_contract(delimiter))
    assert report.complete and not report.ok
    assert report.records == 2
    assert report.error_count == 2
    assert [issue.code for issue in report.issues] == ["contract_null", "contract_null"]
    assert all(issue.record == 2 and issue.line_start == 2 for issue in report.issues)


def test_headerless_delimiter_only_record_is_data(make_file, whitespace_delimiter):
    delimiter = whitespace_delimiter
    path = make_file(f"{delimiter}\n1{delimiter}Ada\n")
    report = inspect_file(path, contract=_contract(delimiter, nullable=True, header=False))
    assert report.ok and report.records == 2 and report.columns == 2


@pytest.mark.parametrize("engine", ["pandas", "polars"])
def test_nullable_empty_record_survives_dataframe_import(make_file, whitespace_delimiter, engine):
    pytest.importorskip(engine)
    delimiter = whitespace_delimiter
    path = make_file(f"id{delimiter}name\n{delimiter}\n1{delimiter}Ada\n")
    frame = getattr(adapters, f"read_{engine}")(path, contract=_contract(delimiter, nullable=True))
    records = frame.to_dict(orient="records") if engine == "pandas" else frame.to_dicts()
    assert records == [{"id": "", "name": ""}, {"id": "1", "name": "Ada"}]


def test_normalization_preserves_empty_record_and_reports_it(
    make_file, tmp_path, whitespace_delimiter
):
    delimiter = whitespace_delimiter
    text = f"id{delimiter}name\r\n{delimiter}\r\n1{delimiter}Ada\r\n"
    path = make_file(text)
    output = tmp_path / "normalized.csv"
    result = normalize_file(path, output, contract=_contract(delimiter, nullable=True))
    assert result.report.records == 2 and result.report.ok
    assert output.read_bytes() == path.read_bytes() == text.encode("utf-8")


def test_actual_blank_lines_are_still_ignored(make_file):
    path = make_file("\nid\tname\n\n \n1\tAda\n\n", name="input.tsv")
    report = inspect_file(path, contract=_contract("\t"))
    assert report.ok and report.records == 1 and report.physical_lines == 6
