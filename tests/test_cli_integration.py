"""Subprocess integration checks for contracts and safe UTF-8 normalization."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "ingest_sentry", *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
        timeout=30,
    )


def _contract(make_file, **overrides):
    payload = {
        "columns": [{"name": "private_id", "type": "integer"}, {"name": "private_name"}],
        "encoding": "utf-8",
        "delimiter": ",",
    }
    payload.update(overrides)
    return make_file(json.dumps(payload), name="private-contract-folder/import.json")


def _assert_redacted(result, *private_values):
    for value in private_values:
        assert str(value) not in result.stdout
        assert str(value) not in result.stderr
    assert "Traceback" not in result.stderr


def test_inspect_contract_accepts_clean_rows_and_redacts_configuration(make_file):
    path = make_file(
        "private_id,private_name\n0001,SECRET_CLIENT\n", name="private-data-folder/input.csv"
    )
    contract = _contract(make_file)
    result = _run("inspect", path, "--contract", contract, "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["policy_passed"] is True
    assert payload["records"] == 1
    assert payload["encoding"]["origin"] == "provided"
    assert payload["dialect"]["delimiter"] == ","
    assert payload["issues"] == []
    assert result.stderr == ""
    _assert_redacted(
        result,
        path,
        contract,
        "private_id",
        "private_name",
        "SECRET_CLIENT",
        "private-data-folder",
        "private-contract-folder",
    )


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_inspect_contract_type_error_is_redacted(make_file, output_format):
    path = make_file("private_id,private_name\nSECRET_BAD_INTEGER,SECRET_CLIENT\n")
    result = _run("inspect", path, "--contract", _contract(make_file), "--format", output_format)

    assert result.returncode == 1
    assert result.stderr == ""
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["policy_passed"] is False
        assert payload["error_count"] == 1
        assert payload["issues"][0]["code"] == "contract_type"
        assert payload["issues"][0]["record"] == 2
    else:
        assert "ERROR contract_type; record 2; physical lines 2-2" in result.stdout
    _assert_redacted(result, "private_id", "private_name", "SECRET_BAD_INTEGER", "SECRET_CLIENT")


def test_fail_on_warning_changes_policy_without_rewriting_inspection_ok(make_file):
    path = make_file("id,name\n")
    regular = _run("inspect", path, "--encoding", "utf-8", "--format", "json")
    strict = _run("inspect", path, "--encoding", "utf-8", "--fail-on-warning", "--format", "json")

    assert regular.returncode == 0
    assert strict.returncode == 1
    before = json.loads(regular.stdout)
    after = json.loads(strict.stdout)
    assert before["ok"] is after["ok"] is True
    assert before["warning_count"] == after["warning_count"] == 1
    assert before["policy_passed"] is True
    assert after["policy_passed"] is False
    before.pop("policy_passed")
    after.pop("policy_passed")
    assert before == after


def test_fail_on_warning_text_explains_policy_failure(make_file):
    result = _run("inspect", make_file("id,name\n"), "--encoding", "utf-8", "--fail-on-warning")
    assert result.returncode == 1
    assert "Result: OK" in result.stdout
    assert "Policy: FAILED (--fail-on-warning)." in result.stdout


@pytest.mark.parametrize("command", ["inspect", "normalize"])
def test_contract_header_false_resolves_without_no_header_flag(make_file, tmp_path, command):
    source = make_file("0001;SECRET_CLIENT\n0002;SECOND_CLIENT\n")
    contract = _contract(make_file, header=False, delimiter=";")
    args = [command, source, "--contract", contract, "--format", "json"]
    output = tmp_path / "normalized.csv"
    if command == "normalize":
        args.extend(["--output", output])
    result = _run(*args)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    report = payload["inspection"] if command == "normalize" else payload
    assert report["records"] == 2
    assert report["dialect"]["header"] is False
    assert report["dialect"]["delimiter"] == ";"
    if command == "normalize":
        assert output.read_bytes() == source.read_bytes()
    _assert_redacted(result, "SECRET_CLIENT", "SECOND_CLIENT")


@pytest.mark.parametrize("command", ["normalize", "fix"])
@pytest.mark.parametrize("encoding", ["cp1254", "utf-16"])
def test_normalize_and_fix_preserve_text_and_publish_utf8(make_file, tmp_path, command, encoding):
    content = 'id,name,note\r\n0001,Çağrı,"multi\r\nline"\r\n0002,İpek,""\r\n'
    source = make_file(content, encoding=encoding, name="private-source-folder/input.csv")
    original = source.read_bytes()
    output = tmp_path / "normalized.csv"
    result = _run(
        command,
        source,
        "--output",
        output,
        "--encoding",
        encoding,
        "--delimiter",
        ",",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "output", "output_bytes", "output_sha256", "inspection"}
    expected = content.encode("utf-8")
    assert output.read_bytes() == expected
    assert source.read_bytes() == original
    assert payload["ok"] is True
    assert payload["output"] == output.name
    assert payload["output_bytes"] == len(expected)
    assert payload["output_sha256"] == hashlib.sha256(expected).hexdigest()
    assert payload["inspection"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert payload["inspection"]["ok"] is True
    assert payload["inspection"]["records"] == 2
    _assert_redacted(result, source, output, "private-source-folder", "multi", "0001")
    assert not list(tmp_path.glob(".ingest-sentry-*.tmp"))


def test_normalize_text_summary_contains_only_redacted_metadata(make_file, tmp_path):
    source = make_file("private_id,private_name\n0001,SECRET_CLIENT\n")
    output = tmp_path / "normalized.csv"
    result = _run("normalize", source, "--output", output, "--encoding", "utf-8")

    assert result.returncode == 0
    assert "UTF-8 output: normalized.csv" in result.stdout
    assert f"Output bytes: {output.stat().st_size}" in result.stdout
    assert hashlib.sha256(output.read_bytes()).hexdigest() in result.stdout
    _assert_redacted(result, source, output, "private_id", "private_name", "SECRET_CLIENT")


@pytest.mark.parametrize("command", ["normalize", "fix"])
def test_existing_destination_is_never_overwritten(make_file, command):
    source = make_file("id,name\n1,Ada\n")
    destination = make_file("SECRET_DESTINATION_CONTENT", name="private-output-folder/result.csv")
    original = source.read_bytes()
    result = _run(command, source, "--output", destination, "--encoding", "utf-8")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "output already exists" in result.stderr
    assert destination.read_text() == "SECRET_DESTINATION_CONTENT"
    assert source.read_bytes() == original
    _assert_redacted(
        result, source, destination, "SECRET_DESTINATION_CONTENT", "private-output-folder"
    )


def test_source_cannot_be_used_as_destination(make_file):
    source = make_file("id,name\n1,Ada\n")
    original = source.read_bytes()
    result = _run("normalize", source, "--output", source, "--encoding", "utf-8")
    assert result.returncode == 2
    assert source.read_bytes() == original


@pytest.mark.parametrize("command", ["normalize", "fix"])
def test_invalid_input_produces_failure_envelope_and_no_output(make_file, tmp_path, command):
    source = make_file("private_id,private_name\nSECRET_BAD_ROW\n")
    original = source.read_bytes()
    destination = tmp_path / "result.csv"
    result = _run(
        command, source, "--output", destination, "--encoding", "utf-8", "--format", "json"
    )

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "error", "inspection"}
    assert payload["ok"] is False
    assert payload["inspection"]["ok"] is False
    assert payload["inspection"]["issues"][0]["code"] == "column_count"
    assert not destination.exists()
    assert source.read_bytes() == original
    _assert_redacted(result, source, destination, "private_id", "private_name", "SECRET_BAD_ROW")


def test_normalize_contract_failure_keeps_redacted_inspection(make_file, tmp_path):
    source = make_file("private_id,private_name\nSECRET_BAD_INTEGER,SECRET_CLIENT\n")
    destination = tmp_path / "result.csv"
    result = _run(
        "normalize",
        source,
        "--output",
        destination,
        "--contract",
        _contract(make_file),
        "--format",
        "json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["inspection"]["issues"][0]["code"] == "contract_type"
    assert not destination.exists()
    _assert_redacted(result, "private_id", "private_name", "SECRET_BAD_INTEGER", "SECRET_CLIENT")


def test_ambiguous_input_requires_explicit_opt_in(make_file, tmp_path):
    source = make_file(b"id,name\n1,\xe9\n")
    output = tmp_path / "result.csv"
    denied = _run("normalize", source, "--output", output, "--format", "json")

    assert denied.returncode == 1
    refused = json.loads(denied.stdout)
    assert refused["ok"] is False
    assert refused["inspection"]["ok"] is True
    assert refused["inspection"]["encoding"]["status"] == "ambiguous"
    assert "Ambiguous encoding" in refused["error"]
    assert not output.exists()

    accepted = _run(
        "normalize", source, "--output", output, "--accept-ambiguous", "--format", "json"
    )
    assert accepted.returncode == 0, accepted.stderr
    payload = json.loads(accepted.stdout)
    assert payload["inspection"]["encoding"]["status"] == "ambiguous"
    codec = payload["inspection"]["encoding"]["name"]
    assert output.read_bytes() == source.read_bytes().decode(codec).encode("utf-8")


@pytest.mark.parametrize("command", ["inspect", "normalize"])
@pytest.mark.parametrize(
    "contract_text",
    [
        "{",
        '{"columns": [], "PRIVATE_EXTRA_KEY": "SECRET_CONFIG"}',
        '{"columns": [{"name": "id", "type": "PRIVATE_UNKNOWN_TYPE"}]}',
        '{"columns": [{"name": "id"}], "header": "PRIVATE_HEADER_VALUE"}',
    ],
)
def test_invalid_contracts_exit_two_without_configuration_disclosure(
    make_file, tmp_path, command, contract_text
):
    source = make_file("id\n1\n")
    contract = make_file(contract_text, name="private-contract-folder/config.json")
    output = tmp_path / "output.csv"
    args = [command, source, "--contract", contract, "--format", "json"]
    if command == "normalize":
        args.extend(["--output", output])
    result = _run(*args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr
    assert not output.exists()
    _assert_redacted(
        result,
        source,
        contract,
        "PRIVATE_EXTRA_KEY",
        "SECRET_CONFIG",
        "PRIVATE_UNKNOWN_TYPE",
        "PRIVATE_HEADER_VALUE",
        "private-contract-folder",
    )


@pytest.mark.parametrize("args", [["--encoding", "cp1254"], ["--delimiter", ";"], ["--no-header"]])
def test_options_conflicting_with_contract_exit_two(make_file, tmp_path, args):
    source = make_file("private_id,private_name\n1,Ada\n")
    output = tmp_path / "output.csv"
    result = _run(
        "normalize", source, "--output", output, "--contract", _contract(make_file), *args
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert not output.exists()
    _assert_redacted(result, source, output)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-output-bytes", "0"],
        ["--max-output-bytes", "PRIVATE_INTEGER"],
        ["--encoding", "PRIVATE_CODEC"],
        ["--format", "PRIVATE_FORMAT"],
        ["--PRIVATE_FLAG", "PRIVATE_VALUE"],
    ],
)
def test_normalize_invalid_arguments_do_not_echo_user_values(make_file, tmp_path, arguments):
    source = make_file("id,name\n1,Ada\n", name="private-folder/input.csv")
    output = tmp_path / "result.csv"
    result = _run("normalize", source, "--output", output, *arguments)
    assert result.returncode == 2
    assert result.stdout == ""
    assert not output.exists()
    _assert_redacted(result, source, output, "PRIVATE_", "private-folder")


def test_unknown_subcommand_does_not_echo_argument():
    result = _run("PRIVATE_COMMAND")
    assert result.returncode == 2
    _assert_redacted(result, "PRIVATE_COMMAND")


def test_output_limit_refuses_publication_and_removes_temporary_file(make_file, tmp_path):
    source = make_file("id,name\n1,Çağrı\n", encoding="cp1254")
    original = source.read_bytes()
    output = tmp_path / "output.csv"
    result = _run(
        "normalize",
        source,
        "--output",
        output,
        "--encoding",
        "cp1254",
        "--max-output-bytes",
        "8",
        "--format",
        "json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["inspection"]["ok"] is True
    assert "max_output_bytes" in payload["error"]
    assert not output.exists()
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".ingest-sentry-*.tmp"))


def test_missing_contract_path_is_redacted(make_file, tmp_path):
    source = make_file("id\n1\n")
    contract = tmp_path / "PRIVATE_CONTRACT_DIRECTORY" / "config.json"
    result = _run("inspect", source, "--contract", contract)
    assert result.returncode == 2
    assert result.stdout == ""
    _assert_redacted(result, contract, "PRIVATE_CONTRACT_DIRECTORY")


def test_missing_output_directory_is_redacted(make_file, tmp_path):
    source = make_file("id\n1\n")
    output = tmp_path / "PRIVATE_OUTPUT_DIRECTORY" / "result.csv"
    result = _run("normalize", source, "--output", output, "--encoding", "utf-8")
    assert result.returncode == 2
    assert result.stdout == ""
    assert not output.exists()
    _assert_redacted(result, source, output, "PRIVATE_OUTPUT_DIRECTORY")
