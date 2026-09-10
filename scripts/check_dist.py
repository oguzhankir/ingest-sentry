"""Check release payloads without installing or executing their code."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def check(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"))
    sources = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    required = {
        "ingest_sentry/__init__.py",
        "ingest_sentry/__main__.py",
        "ingest_sentry/cli.py",
        "ingest_sentry/inspection.py",
        "ingest_sentry/models.py",
        "ingest_sentry/contracts.py",
        "ingest_sentry/errors.py",
        "ingest_sentry/normalization.py",
        "ingest_sentry/adapters.py",
        "ingest_sentry/py.typed",
    }
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        if not required <= names:
            raise ValueError("wheel is missing package files")
        if any(name.endswith((".so", ".dll", ".pyd")) for name in names):
            raise ValueError("ingest-sentry's own wheel must remain pure Python")
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError("wheel must have one METADATA file")
        metadata = BytesParser().parsebytes(wheel.read(metadata_files[0]))
        version = metadata["Version"]
        if metadata["Name"] != "ingest-sentry" or metadata["Requires-Python"] != ">=3.10":
            raise ValueError("unexpected package name or Python requirement")
        if metadata["License-Expression"] != "MIT":
            raise ValueError("MIT license metadata is missing")
        info = metadata_files[0].rsplit("/", 1)[0]
        if f"{info}/licenses/LICENSE" not in names:
            raise ValueError("license text is missing from the wheel")
        entry_points = wheel.read(f"{info}/entry_points.txt").decode("utf-8")
        if "ingest-sentry = ingest_sentry.cli:main" not in entry_points:
            raise ValueError("CLI entry point is missing")
        runtime = [dep for dep in metadata.get_all("Requires-Dist", []) if "extra ==" not in dep]
        if len(runtime) != 1 or runtime[0] not in {"bytesense<2,>=1.1.0", "bytesense>=1.1.0,<2"}:
            raise ValueError("unexpected runtime dependencies")
    with tarfile.open(sources[0], "r:gz") as source:
        source_names = set(source.getnames())
        prefix = f"ingest_sentry-{version}/"
        expected = {prefix + "src/" + name for name in required}
        expected.update(
            prefix + name
            for name in (
                "README.md",
                "LICENSE",
                "pyproject.toml",
                "action.yml",
                "scripts/action_runner.py",
                "examples/vendor.contract.json",
                "examples/vendor_export.csv",
                "examples/vendor_export_broken.csv",
            )
        )
        if not expected <= source_names:
            raise ValueError("source distribution is missing required files")
        if not any(name.startswith(prefix + "tests/") for name in source_names):
            raise ValueError("source distribution must include the tests")
        if prefix + "scripts/check_dist.py" not in source_names:
            raise ValueError("source distribution must include this validator")
    print(f"Validated wheel and source distribution for ingest-sentry {version}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    check(args.directory)


if __name__ == "__main__":
    main()
