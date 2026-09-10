"""Isolated GitHub Action setup and offline, content-redacted inspection.

Only dependency setup accesses the network. Input values are environment data,
never shell fragments. Reports stay in RUNNER_TEMP and are not uploaded.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
import venv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

_PREFIX = "INGEST_SENTRY_INPUT_"


@dataclass(frozen=True)
class Options:
    path: Path
    contract: Path | None
    encoding: str | None
    delimiter: str | None
    header: bool | None
    max_bytes: int
    max_issues: int
    fail_on_warning: bool


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("Boolean inputs must be true or false.")
    return normalized == "true"


def _positive_integer(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Limits must be positive decimal integers.")
    return int(value)


def _directory(env: Mapping[str, str], name: str) -> Path:
    value = env.get(name, "")
    if not value or not Path(value).is_absolute():
        raise ValueError("Required runner directory is unavailable.")
    path = Path(value).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Required runner directory is unavailable.")
    return path


def _local_file(value: str, workspace: Path) -> Path:
    candidate = Path(value)
    if (
        not value
        or "\x00" in value
        or "://" in value
        or candidate.is_absolute()
        or PureWindowsPath(value).drive
        or ".." in candidate.parts
        or ".." in PureWindowsPath(value).parts
    ):
        raise ValueError("Inputs must name workspace-relative regular files.")
    path = (workspace / candidate).resolve(strict=True)
    if not path.is_relative_to(workspace) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Inputs must name workspace-relative regular files.")
    return path


def parse_options(env: Mapping[str, str]) -> Options:
    """Validate environment inputs without importing or installing the package."""
    workspace = _directory(env, "GITHUB_WORKSPACE")
    path = _local_file(env.get(_PREFIX + "PATH", ""), workspace)
    contract_value = env.get(_PREFIX + "CONTRACT", "")
    contract = _local_file(contract_value, workspace) if contract_value else None
    delimiter = env.get(_PREFIX + "DELIMITER", "") or None
    if delimiter == "\\t":
        delimiter = "\t"
    if delimiter is not None and (
        len(delimiter) != 1
        or delimiter in '\r\n\x00"'
        or (not delimiter.isprintable() and delimiter != "\t")
    ):
        raise ValueError("Invalid delimiter.")
    return Options(
        path=path,
        contract=contract,
        encoding=env.get(_PREFIX + "ENCODING", "") or None,
        delimiter=delimiter,
        header=False if _boolean(env.get(_PREFIX + "NO_HEADER", "false")) else None,
        max_bytes=_positive_integer(env.get(_PREFIX + "MAX_BYTES", "67108864")),
        max_issues=_positive_integer(env.get(_PREFIX + "MAX_ISSUES", "100")),
        fail_on_warning=_boolean(env.get(_PREFIX + "FAIL_ON_WARNING", "false")),
    )


def _error_report(code: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "ok": False,
        "complete": False,
        "error_count": 1,
        "warning_count": 0,
        "issue_count": 1,
        "issues": [
            {
                "code": code,
                "severity": "error",
                "message": "Action could not complete; check local inputs and runner setup.",
            }
        ],
    }


def _write_outputs(env: Mapping[str, str], outputs: Mapping[str, str]) -> None:
    """Use delimited output values so even unusual runner paths remain data."""
    target = env.get("GITHUB_OUTPUT")
    if not target:
        raise ValueError("GitHub output file is unavailable.")
    blocks = []
    for name, value in outputs.items():
        delimiter = "ingest_sentry_" + uuid.uuid4().hex
        while delimiter in value.splitlines():
            delimiter = "ingest_sentry_" + uuid.uuid4().hex
        blocks.append(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
    with Path(target).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(blocks))


def finish(env: Mapping[str, str], report: dict[str, Any], exit_code: int) -> int:
    """Persist a private report before returning a failing step exit code."""
    report["action_passed"] = exit_code == 0
    try:
        temporary = _directory(env, "RUNNER_TEMP")
        directory = Path(tempfile.mkdtemp(prefix="ingest-sentry-report-", dir=temporary))
        target = directory / "report.json"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
        except Exception:
            target.unlink(missing_ok=True)
            directory.rmdir()
            raise
        _write_outputs(
            env,
            {
                "report-path": str(target),
                "ok": str(exit_code == 0).lower(),
                "error-count": str(report["error_count"]),
                "warning-count": str(report["warning_count"]),
            },
        )
    except (OSError, ValueError):
        print("::error::Ingest Sentry could not save its private report or action outputs.")
        return 2
    if exit_code:
        print("::error::Ingest Sentry import check failed; inspect the private JSON report.")
    else:
        print("Ingest Sentry import check passed.")
    print(f"Errors: {report['error_count']}; warnings: {report['warning_count']}.")
    return exit_code


def run_inspection(env: Mapping[str, str]) -> int:
    """Inspect locally without subprocesses, telemetry, or network requests."""
    try:
        from ingest_sentry import inspect_file
        from ingest_sentry.contracts import load_contract

        options = parse_options(env)
        contract = load_contract(options.contract) if options.contract else None
        report = inspect_file(
            options.path,
            contract=contract,
            encoding=options.encoding,
            delimiter=options.delimiter,
            header=options.header,
            max_bytes=options.max_bytes,
            max_issues=options.max_issues,
        )
        accepted = report.ok and not (options.fail_on_warning and report.warning_count)
        return finish(env, report.to_dict(), 0 if accepted else 1)
    except (ValueError, LookupError):
        return finish(env, _error_report("action_invalid_options"), 2)
    except OSError:
        return finish(env, _error_report("action_input_io"), 2)
    except Exception:
        # Exception strings and tracebacks may contain filenames or private data.
        return finish(env, _error_report("action_internal_error"), 2)


def _install_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Preserve explicit index settings, not caller-controlled install destinations."""
    result = dict(env)
    for name in (
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_ROOT",
        "PIP_USER",
        "PIP_PYTHON",
        "PIP_LOG",
        "PIP_REPORT",
    ):
        result.pop(name, None)
    # pip documents os.devnull as disabling global, user, and site config files.
    result["PIP_CONFIG_FILE"] = os.devnull
    return result


def bootstrap(env: Mapping[str, str]) -> int:
    """Install this action revision into a disposable, isolated environment."""
    try:
        parse_options(env)
        temporary = _directory(env, "RUNNER_TEMP")
        action_path = _directory(env, "INGEST_SENTRY_ACTION_PATH")
        helper = action_path / "scripts" / "action_runner.py"
        if not helper.is_file() or not (action_path / "pyproject.toml").is_file():
            raise ValueError("Action source is incomplete.")
        print("Preparing an isolated Ingest Sentry environment.", flush=True)
        with tempfile.TemporaryDirectory(prefix="ingest-sentry-venv-", dir=temporary) as name:
            environment = Path(name) / "venv"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            install = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-user",
                    str(action_path),
                ],
                env=_install_environment(env),
                capture_output=True,
                check=False,
            )
            if install.returncode:
                return finish(env, _error_report("action_install_failed"), 2)
            # Isolated mode also prevents workspace/PYTHONPATH import shadowing.
            print("Inspecting the local input without uploading data.", flush=True)
            result = subprocess.run(
                [str(python), "-I", str(helper), "--inspect"],
                env=dict(env),
                check=False,
            )
            if result.returncode not in {0, 1, 2}:
                return finish(env, _error_report("action_process_failed"), 2)
            return result.returncode
    except (ValueError, LookupError):
        return finish(env, _error_report("action_invalid_options"), 2)
    except OSError:
        return finish(env, _error_report("action_setup_io"), 2)
    except Exception:
        return finish(env, _error_report("action_setup_failed"), 2)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--inspect"]:
        return run_inspection(os.environ)
    if not args:
        return bootstrap(os.environ)
    print("::error::Ingest Sentry received an unsupported internal invocation.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
