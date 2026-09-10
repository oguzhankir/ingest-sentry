"""The action treats all caller-controlled inputs as data and keeps reports local."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "ingest_sentry_action_runner", _ROOT / "scripts" / "action_runner.py"
)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


@pytest.fixture
def action_env(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.csv").write_bytes(b"id,name\n1,Ada\n2,Grace\n")
    temporary = tmp_path / "runner"
    temporary.mkdir()
    output = tmp_path / "outputs"
    output.touch()
    return {
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(temporary),
        "GITHUB_OUTPUT": str(output),
        "INGEST_SENTRY_ACTION_PATH": str(_ROOT),
        "INGEST_SENTRY_INPUT_PATH": "input.csv",
        "INGEST_SENTRY_INPUT_ENCODING": "utf-8",
        "INGEST_SENTRY_INPUT_DELIMITER": ",",
    }


def output_values(env: dict[str, str]) -> dict[str, str]:
    lines = Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8").splitlines()
    result = {}
    index = 0
    while index < len(lines):
        key, delimiter = lines[index].split("<<", 1)
        index += 1
        value = []
        while lines[index] != delimiter:
            value.append(lines[index])
            index += 1
        result[key] = "\n".join(value)
        index += 1
    return result


def report_data(env: dict[str, str]) -> dict:
    return json.loads(Path(output_values(env)["report-path"]).read_text(encoding="utf-8"))


def test_options_defaults(action_env: dict[str, str]) -> None:
    options = runner.parse_options(action_env)
    assert options.path == Path(action_env["GITHUB_WORKSPACE"]) / "input.csv"
    assert options.contract is None
    assert options.encoding == "utf-8"
    assert options.delimiter == ","
    assert options.header is None
    assert options.max_bytes == 67108864
    assert options.max_issues == 100
    assert options.fail_on_warning is False


def test_options_overrides(action_env: dict[str, str]) -> None:
    contract = Path(action_env["GITHUB_WORKSPACE"]) / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    action_env.update(
        INGEST_SENTRY_INPUT_CONTRACT="contract.json",
        INGEST_SENTRY_INPUT_DELIMITER="\\t",
        INGEST_SENTRY_INPUT_NO_HEADER="TRUE",
        INGEST_SENTRY_INPUT_MAX_BYTES="1024",
        INGEST_SENTRY_INPUT_MAX_ISSUES="8",
        INGEST_SENTRY_INPUT_FAIL_ON_WARNING="true",
    )
    options = runner.parse_options(action_env)
    assert options.contract == contract
    assert options.delimiter == "\t"
    assert options.header is False
    assert options.max_bytes == 1024
    assert options.max_issues == 8
    assert options.fail_on_warning is True


@pytest.mark.parametrize("value", ["", "yes", "0", "1", "false\n::error::injected"])
def test_invalid_booleans(value: str) -> None:
    with pytest.raises(ValueError):
        runner._boolean(value)


@pytest.mark.parametrize("value", ["", "-1", "0", "1.5", "1_000", "１２", "1; whoami"])
def test_invalid_limits(value: str) -> None:
    with pytest.raises(ValueError):
        runner._positive_integer(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../input.csv",
        "nested/../../input.csv",
        "https://example.com/data.csv",
        "C:\\private.csv",
        "C:private.csv",
        "\\\\server\\share\\data.csv",
        "\x00",
    ],
)
def test_input_path_rejects_external_targets(action_env: dict[str, str], value: str) -> None:
    action_env["INGEST_SENTRY_INPUT_PATH"] = value
    with pytest.raises((ValueError, OSError)):
        runner.parse_options(action_env)


@pytest.mark.parametrize("value", ['"', "\n", "\x00", "::error::x", "\x1b"])
def test_invalid_delimiter(action_env: dict[str, str], value: str) -> None:
    action_env["INGEST_SENTRY_INPUT_DELIMITER"] = value
    with pytest.raises(ValueError):
        runner.parse_options(action_env)


def test_absolute_path_rejected_even_inside_workspace(action_env: dict[str, str]) -> None:
    action_env["INGEST_SENTRY_INPUT_PATH"] = str(Path(action_env["GITHUB_WORKSPACE"]) / "input.csv")
    with pytest.raises(ValueError):
        runner.parse_options(action_env)


def test_symlink_escape_rejected(action_env: dict[str, str], tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"id\n1\n")
    link = Path(action_env["GITHUB_WORKSPACE"]) / "link.csv"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Runner does not allow test symlinks.")
    action_env["INGEST_SENTRY_INPUT_PATH"] = "link.csv"
    with pytest.raises(ValueError):
        runner.parse_options(action_env)


def test_directory_input_rejected(action_env: dict[str, str]) -> None:
    action_env["INGEST_SENTRY_INPUT_PATH"] = "."
    with pytest.raises(ValueError):
        runner.parse_options(action_env)


def test_report_is_private_local_and_input_is_unchanged(action_env: dict[str, str]) -> None:
    source = Path(action_env["GITHUB_WORKSPACE"]) / "input.csv"
    before = source.read_bytes()
    assert runner.run_inspection(action_env) == 0
    outputs = output_values(action_env)
    target = Path(outputs["report-path"])
    assert target.is_relative_to(Path(action_env["RUNNER_TEMP"]))
    assert target.parent.name.startswith("ingest-sentry-report-")
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert source.read_bytes() == before
    assert outputs["ok"] == "true"
    assert outputs["error-count"] == "0"
    assert outputs["warning-count"] == "0"
    report = target.read_text(encoding="utf-8")
    assert '"action_passed": true' in report
    assert "Ada" not in report
    assert "Grace" not in report


def test_failed_report_outputs_survive(action_env: dict[str, str]) -> None:
    source = Path(action_env["GITHUB_WORKSPACE"]) / "input.csv"
    source.write_bytes(b"secret_header,other_header\nprivate-value,2,3\n")
    assert runner.run_inspection(action_env) == 1
    assert output_values(action_env)["ok"] == "false"
    report = report_data(action_env)
    assert report["error_count"] == 1
    assert report["action_passed"] is False
    assert "private-value" not in json.dumps(report)
    assert "secret_header" not in json.dumps(report)


@pytest.mark.parametrize("fail, expected", [("false", 0), ("true", 1)])
def test_warning_policy(action_env: dict[str, str], fail: str, expected: int) -> None:
    source = Path(action_env["GITHUB_WORKSPACE"]) / "input.csv"
    source.write_bytes(b"id,id\n1,Ada\n")
    action_env["INGEST_SENTRY_INPUT_FAIL_ON_WARNING"] = fail
    assert runner.run_inspection(action_env) == expected
    report = report_data(action_env)
    assert report["ok"] is True
    assert report["warning_count"] > 0
    assert report["action_passed"] is (expected == 0)


def test_invalid_options_never_log_input(
    action_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    action_env["INGEST_SENTRY_INPUT_DELIMITER"] = "\n::error::private-value"
    assert runner.run_inspection(action_env) == 2
    assert output_values(action_env)["ok"] == "false"
    assert "private-value" not in capsys.readouterr().out
    assert report_data(action_env)["issues"][0]["code"] == "action_invalid_options"


def test_missing_input_report(action_env: dict[str, str]) -> None:
    action_env["INGEST_SENTRY_INPUT_PATH"] = "missing.csv"
    assert runner.run_inspection(action_env) == 2
    assert report_data(action_env)["issues"][0]["code"] == "action_input_io"


def test_contract_enforcement_and_redaction(action_env: dict[str, str]) -> None:
    workspace = Path(action_env["GITHUB_WORKSPACE"])
    (workspace / "input.csv").write_text(
        "id,private-header\nwrong-integer,private-value\n", encoding="utf-8"
    )
    (workspace / "contract.json").write_text(
        json.dumps(
            {
                "version": 1,
                "encoding": "utf-8",
                "delimiter": ",",
                "columns": [
                    {"name": "id", "type": "integer"},
                    {"name": "private-header", "type": "string"},
                ],
            }
        ),
        encoding="utf-8",
    )
    action_env["INGEST_SENTRY_INPUT_CONTRACT"] = "contract.json"
    assert runner.run_inspection(action_env) == 1
    report = report_data(action_env)
    assert any(issue["code"] == "contract_type" for issue in report["issues"])
    text = json.dumps(report)
    assert "private-value" not in text
    assert "private-header" not in text
    assert "wrong-integer" not in text


def test_contract_header_setting_is_not_overridden_by_defaults(
    action_env: dict[str, str],
) -> None:
    workspace = Path(action_env["GITHUB_WORKSPACE"])
    (workspace / "input.csv").write_bytes(b"1,Ada\n2,Grace\n")
    (workspace / "contract.json").write_text(
        json.dumps(
            {
                "version": 1,
                "encoding": "utf-8",
                "delimiter": ",",
                "header": False,
                "columns": [{"name": "id", "type": "integer"}, {"name": "name", "type": "string"}],
            }
        ),
        encoding="utf-8",
    )
    action_env["INGEST_SENTRY_INPUT_CONTRACT"] = "contract.json"
    assert runner.run_inspection(action_env) == 0
    assert report_data(action_env)["records"] == 2


def test_malformed_contract_does_not_log_fields(
    action_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Path(action_env["GITHUB_WORKSPACE"])
    (workspace / "contract.json").write_text(
        '{"private-value": "\\n::error::injected"}', encoding="utf-8"
    )
    action_env["INGEST_SENTRY_INPUT_CONTRACT"] = "contract.json"
    assert runner.run_inspection(action_env) == 2
    assert "private-value" not in capsys.readouterr().out
    assert "private-value" not in json.dumps(report_data(action_env))


def test_shell_metacharacters_are_only_a_filename(
    action_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = Path(action_env["GITHUB_WORKSPACE"])
    name = "$(touch INJECTED).csv"
    (workspace / name).write_bytes(b"id,name\n1,private-value\n")
    action_env["INGEST_SENTRY_INPUT_PATH"] = name
    assert runner.run_inspection(action_env) == 0
    assert not (workspace / "INJECTED").exists()
    assert name not in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline in filenames.")
def test_annotation_injection_filename_is_not_logged(
    action_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    name = "input\n::error::private-value.csv"
    (Path(action_env["GITHUB_WORKSPACE"]) / name).write_bytes(b"id\n1\n")
    action_env["INGEST_SENTRY_INPUT_PATH"] = name
    assert runner.run_inspection(action_env) == 0
    assert "::error::private-value" not in capsys.readouterr().out
    assert report_data(action_env)["source"] == name


def test_output_values_use_delimited_protocol(action_env: dict[str, str]) -> None:
    values = {"report-path": "directory\nforged=true\n::error::nothing", "ok": "false"}
    runner._write_outputs(action_env, values)
    assert output_values(action_env) == values
    assert "forged" not in output_values(action_env)


def test_missing_output_file_fails_closed(
    action_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    action_env.pop("GITHUB_OUTPUT")
    assert runner.finish(action_env, runner._error_report("test"), 1) == 2
    assert "could not save" in capsys.readouterr().out


def test_invalid_runner_temp_does_not_write_to_workspace(action_env: dict[str, str]) -> None:
    action_env["RUNNER_TEMP"] = "relative-directory"
    assert runner.finish(action_env, runner._error_report("test"), 1) == 2
    assert list(Path(action_env["GITHUB_WORKSPACE"]).iterdir()) == [
        Path(action_env["GITHUB_WORKSPACE"]) / "input.csv"
    ]


def test_report_serialization_failure_cleans_only_own_directory(
    action_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = Path(action_env["RUNNER_TEMP"])
    sentinel = temporary / "keep"
    sentinel.write_text("untouched", encoding="utf-8")
    monkeypatch.setattr(runner.json, "dump", Mock(side_effect=OSError("private-value")))
    assert runner.finish(action_env, runner._error_report("test"), 1) == 2
    assert list(temporary.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_bootstrap_isolated_argv_and_cleanup(
    action_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []
    builder = Mock()
    builder.create.side_effect = lambda path: created.append(path)
    monkeypatch.setattr(runner.venv, "EnvBuilder", Mock(return_value=builder))
    commands = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append((command, kwargs))
        assert "shell" not in kwargs
        assert kwargs["env"] == (
            runner._install_environment(action_env) if "pip" in command else action_env
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.bootstrap(action_env) == 0
    assert len(commands) == 2
    assert commands[0][0][1:5] == ["-I", "-m", "pip", "install"]
    assert commands[0][0][-1] == str(_ROOT)
    assert commands[0][1]["capture_output"] is True
    assert commands[1][0][1:] == ["-I", str(_ROOT / "scripts/action_runner.py"), "--inspect"]
    assert len(created) == 1
    assert created[0].is_relative_to(Path(action_env["RUNNER_TEMP"]))
    assert not created[0].parent.exists()


def test_pip_destination_overrides_cannot_escape_the_venv() -> None:
    env = {
        "PIP_TARGET": "/consumer/site-packages",
        "PIP_PREFIX": "/consumer",
        "PIP_ROOT": "/another-root",
        "PIP_USER": "true",
        "PIP_PYTHON": "/consumer/python",
        "PIP_REPORT": "/consumer/report.json",
        "PIP_LOG": "/consumer/pip.log",
        "PIP_CONFIG_FILE": "/consumer/pip.conf",
        "PIP_INDEX_URL": "https://packages.example.invalid/simple",
    }
    sanitized = runner._install_environment(env)
    assert sanitized == {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_INDEX_URL": "https://packages.example.invalid/simple",
    }
    assert env["PIP_TARGET"] == "/consumer/site-packages"


def test_bootstrap_install_failure_is_redacted_and_cleans_venv(
    action_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner.venv, "EnvBuilder", Mock(return_value=Mock()))
    install = Mock(return_value=SimpleNamespace(returncode=1, stderr="credential-do-not-log"))
    monkeypatch.setattr(runner.subprocess, "run", install)
    assert runner.bootstrap(action_env) == 2
    assert install.call_count == 1
    assert "credential-do-not-log" not in capsys.readouterr().out
    assert report_data(action_env)["issues"][0]["code"] == "action_install_failed"
    assert not list(Path(action_env["RUNNER_TEMP"]).glob("ingest-sentry-venv-*"))


def test_bootstrap_checks_invalid_inputs_before_installing(
    action_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    action_env["INGEST_SENTRY_INPUT_PATH"] = "../input.csv"
    builder = Mock()
    monkeypatch.setattr(runner.venv, "EnvBuilder", builder)
    assert runner.bootstrap(action_env) == 2
    builder.assert_not_called()


def test_internal_exception_does_not_reveal_data(
    action_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ingest_sentry

    monkeypatch.setattr(
        ingest_sentry, "inspect_file", Mock(side_effect=RuntimeError("private-value"))
    )
    assert runner.run_inspection(action_env) == 2
    assert "private-value" not in capsys.readouterr().out
    assert "private-value" not in json.dumps(report_data(action_env))


def test_inspection_does_not_use_network_or_subprocesses(
    action_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = Mock(side_effect=AssertionError("Offline inspection attempted external access."))
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    assert runner.run_inspection(action_env) == 0
    forbidden.assert_not_called()


def test_main_internal_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "bootstrap", Mock(return_value=0))
    monkeypatch.setattr(runner, "run_inspection", Mock(return_value=1))
    assert runner.main([]) == 0
    assert runner.main(["--inspect"]) == 1
    assert runner.main(["--unsupported"]) == 2
