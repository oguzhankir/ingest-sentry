"""Exercise the installed-module CLI as users invoke it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ingest_sentry import cli, inspect_file


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ingest_sentry", *args],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
        timeout=30,
    )


def test_json_clean_file_exits_zero(make_file):
    path = make_file("id,name\n1,Ada\n")
    result = run_cli("inspect", str(path), "--encoding", "utf-8", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["records"] == 1
    assert payload["columns"] == 2
    assert payload["encoding"]["origin"] == "provided"
    assert payload["suggested_read_csv_kwargs"]["encoding_errors"] == "strict"
    assert result.stderr == ""


def test_json_errors_exit_one_with_machine_readable_report(make_file):
    path = make_file("id,name\n1\n")
    result = run_cli(
        "inspect", str(path), "--encoding", "utf-8", "--delimiter", ",", "--format", "json"
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error_count"] == 1
    assert payload["issues"][0]["code"] == "column_count"
    assert payload["suggested_read_csv_kwargs"] is None
    assert result.stderr == ""


def test_json_redacts_parent_path_headers_and_values(make_file):
    path = make_file(
        "private_identifier,private_name\n123456789,VERY_PRIVATE_CLIENT\n",
        name="confidential-export-dir/export.csv",
    )
    result = run_cli("inspect", str(path), "--encoding", "utf-8", "--format", "json")

    assert result.returncode == 0
    assert json.loads(result.stdout)["source"] == "export.csv"
    for private in [
        str(path),
        "confidential-export-dir",
        "private_identifier",
        "123456789",
        "VERY_PRIVATE_CLIENT",
    ]:
        assert private not in result.stdout
        assert private not in result.stderr


def test_default_format_is_human_readable_and_redacted(make_file):
    path = make_file("private_header,name\n123456789,VERY_PRIVATE_CLIENT\n")
    result = run_cli("inspect", str(path), "--encoding", "utf-8")

    assert result.returncode == 0
    assert result.stdout.strip()
    assert "private_header" not in result.stdout
    assert "VERY_PRIVATE_CLIENT" not in result.stdout
    assert "123456789" not in result.stdout
    assert str(path) not in result.stdout


def test_missing_file_exits_two_without_traceback_or_path_disclosure(tmp_path: Path):
    path = tmp_path / "secret-folder" / "missing.csv"
    result = run_cli("inspect", str(path))

    assert result.returncode == 2
    assert result.stderr
    assert "Traceback" not in result.stderr
    assert str(path) not in result.stderr
    assert "secret-folder" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--max-bytes", "0"],
        ["--max-bytes", "-1"],
        ["--max-issues", "0"],
        ["--max-issues", "1.5"],
        ["--delimiter", "::"],
        ["--encoding", "not-a-text-codec"],
        ["--encoding", "base64_codec"],
        ["--format", "xml"],
    ],
)
def test_invalid_options_exit_two(make_file, args):
    path = make_file("id,name\n1,Ada\n")
    result = run_cli("inspect", str(path), *args)

    assert result.returncode == 2
    assert result.stderr
    assert "Traceback" not in result.stderr


def test_explicit_tab_and_no_header_options(make_file):
    path = make_file("1\tAda\n2\tLin\n")
    result = run_cli(
        "inspect",
        str(path),
        "--encoding",
        "utf-8",
        "--delimiter",
        "\\t",
        "--no-header",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["records"] == 2
    assert payload["dialect"]["delimiter"] == "\t"
    assert payload["dialect"]["header"] is False
    assert payload["suggested_read_csv_kwargs"]["header"] is None


def test_byte_limit_exit_one_and_incomplete_json(make_file):
    path = make_file("id,name\n1,Ada\n")
    result = run_cli("inspect", str(path), "--max-bytes", "3", "--format", "json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["complete"] is False
    assert payload["encoding"]["complete"] is False
    assert payload["suggested_read_csv_kwargs"] is None
    assert "file_too_large" in {issue["code"] for issue in payload["issues"]}


def test_version_is_available_without_a_file():
    result = run_cli("--version")

    assert result.returncode == 0
    assert "0.1.0" in result.stdout


def test_help_is_available_without_a_file():
    result = run_cli("--help")

    assert result.returncode == 0
    assert "inspect" in result.stdout


def test_missing_inspect_path_is_usage_error():
    result = run_cli("inspect")

    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_in_process_json_keeps_schema_and_success_exit(make_file, capsys):
    path = make_file("id,name\n1,Ada\n")
    status = cli.main(["inspect", str(path), "--encoding", "utf-8", "--format", "json"])
    captured = capsys.readouterr()

    assert status == 0
    assert json.loads(captured.out)["schema_version"] == "1"
    assert captured.err == ""


def test_in_process_text_lists_redacted_record_spans_and_truncation(make_file, capsys):
    path = make_file('id,note\n1,"PRIVATE\nMULTILINE",extra\n2\n')
    status = cli.main(
        ["inspect", str(path), "--encoding", "utf-8", "--delimiter", ",", "--max-issues", "1"]
    )
    output = capsys.readouterr().out

    assert status == 1
    assert "record 2" in output
    assert "physical lines 2-3" in output
    assert "showing 1 of 2" in output
    assert "PRIVATE" not in output
    assert "MULTILINE" not in output
    assert "LF=4" in output


def test_in_process_text_handles_unknown_encoding_and_absent_dialect(make_file, capsys):
    path = make_file("id,name\n1,Ada\n")
    status = cli.main(["inspect", str(path), "--max-bytes", "1"])
    output = capsys.readouterr().out

    assert status == 1
    assert "Encoding: unknown" in output
    assert "Dialect: unavailable" in output
    assert "snapshot incomplete" in output
    assert "file_too_large" in output


def test_in_process_detection_score_is_labeled_not_probability(make_file, capsys):
    path = make_file("id,name\n1,Ada\n")
    assert cli.main(["inspect", str(path)]) == 0
    output = capsys.readouterr().out

    assert "Detector score:" in output
    assert "not a probability" in output


def test_in_process_safe_filename_cannot_inject_terminal_controls(make_file, monkeypatch, capsys):
    report = inspect_file(make_file("id,name\n1,Ada\n"), encoding="utf-8", delimiter=",")
    report = replace(report, source="export\x1b[31m\n\u202e.csv")
    monkeypatch.setattr(cli, "inspect_file", lambda *args, **kwargs: report)

    assert cli.main(["inspect", "unused.csv"]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert r"export\u001b[31m\n\u202e.csv" in output


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError, PermissionError, IsADirectoryError, OSError, ValueError, LookupError],
)
def test_in_process_errors_do_not_echo_exception_details(make_file, monkeypatch, capsys, error):
    def fail(*args, **kwargs):
        raise error("VERY_PRIVATE_FILENAME_OR_CONTENT")

    monkeypatch.setattr(cli, "inspect_file", fail)
    assert cli.main(["inspect", "unused.csv"]) == 2
    captured = capsys.readouterr()

    assert captured.err
    assert captured.out == ""
    assert "VERY_PRIVATE_FILENAME_OR_CONTENT" not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("error", [BrokenPipeError, OSError])
def test_in_process_output_io_error_returns_two(make_file, monkeypatch, error):
    path = make_file("id,name\n1,Ada\n")

    def fail_output(*args, **kwargs):
        raise error("broken output stream")

    monkeypatch.setattr(cli, "print", fail_output, raising=False)
    assert cli.main(["inspect", str(path), "--encoding", "utf-8"]) == 2


@pytest.mark.parametrize(
    "args",
    [["--max-bytes", "0"], ["--max-issues", "wrong"], ["--delimiter", "::"]],
)
def test_in_process_parser_errors_exit_two(make_file, args):
    path = make_file("id,name\n1,Ada\n")
    with pytest.raises(SystemExit) as result:
        cli.main(["inspect", str(path), *args])
    assert result.value.code == 2


def test_in_process_tab_and_header_options(make_file, capsys):
    path = make_file("1\tAda\n2\tLin\n")
    assert (
        cli.main(["inspect", str(path), "--encoding", "utf-8", "--delimiter", "\\t", "--no-header"])
        == 0
    )
    output = capsys.readouterr().out

    assert 'delimiter="\\t"' in output
    assert "header=no" in output
