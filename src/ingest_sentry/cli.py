"""Local-only command-line inspection with content-redacted diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from . import __version__
from .contracts import load_contract
from .errors import InspectionFailed
from .inspection import inspect_file
from .models import InspectionReport
from .normalization import normalize_file


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default diagnostic echoes untrusted positional/option values.
        self.print_usage(sys.stderr)
        self.exit(2, "ingest-sentry: invalid arguments; see --help for accepted options.\n")


def _positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _delimiter(value: str) -> str:
    if value == "\\t":
        return "\t"
    if len(value) != 1 or value in '\r\n"\x00':
        raise argparse.ArgumentTypeError("must be one character, excluding newline, quote, or NUL")
    return value


def _safe(value: str) -> str:
    """Escape terminal controls, bidi markers, and non-ASCII filename bytes."""
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _text_report(report: InspectionReport) -> str:
    encoding = report.encoding
    rows = [
        f"Source: {_safe(report.source)}",
        f"Result: {'OK' if report.ok else 'FAILED'}",
        f"Observed bytes: {report.byte_count}",
        f"Snapshot SHA-256: {report.sha256 or 'unavailable (snapshot incomplete)'}",
        f"Encoding: {_safe(encoding.name or 'unknown')} ({encoding.origin})",
        f"Decoding: {_safe(encoding.status)}; validated bytes: {encoding.bytes_validated}; "
        f"complete: {'yes' if encoding.complete else 'no'}",
    ]
    if encoding.confidence is not None:
        rows.append(f"Detector score: {encoding.confidence:.6f} (not a probability)")
    rows.append("Strict decoding is not proof that the encoding matches the author's intent.")
    if report.dialect is None:
        rows.append("Dialect: unavailable")
    else:
        dialect = report.dialect
        rows.append(
            f"Dialect: delimiter={json.dumps(dialect.delimiter, ensure_ascii=True)}; "
            f"header={'yes' if dialect.header else 'no'}; "
            f"{'inferred' if dialect.inferred else 'provided'}"
        )
    rows.extend(
        [
            f"Parse complete: {'yes' if report.complete else 'no'}",
            f"Data records: {report.records}; columns: {report.columns}; "
            f"physical lines: {report.physical_lines}",
            "Line endings: "
            + ", ".join(
                f"{kind}={report.line_endings.get(kind, 0)}" for kind in ("LF", "CRLF", "CR")
            ),
            f"Issues: {report.issue_count} ({report.error_count} errors, "
            f"{report.warning_count} warnings)",
        ]
    )
    for issue in report.issues:
        location = ""
        if issue.record is not None:
            location += f"; record {issue.record}"
        if issue.line_start is not None:
            location += f"; physical lines {issue.line_start}-{issue.line_end or issue.line_start}"
        rows.append(
            f"  {issue.severity.upper()} {_safe(issue.code)}{location}: {_safe(issue.message)}"
        )
    if report.issues_truncated:
        rows.append(
            f"Issue details truncated: showing {len(report.issues)} of {report.issue_count}."
        )
    return "\n".join(rows)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="ingest-sentry", description="Inspect CSV/TSV imports locally without modifying data."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="validate a local CSV/TSV file")
    _common_arguments(inspect)
    inspect.add_argument(
        "--fail-on-warning", action="store_true", help="exit 1 when the report contains warnings"
    )
    normalize = subparsers.add_parser(
        "normalize", aliases=["fix"], help="transcode validated input to a new UTF-8 file"
    )
    _common_arguments(normalize)
    normalize.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="new destination; existing files are never overwritten",
    )
    normalize.add_argument(
        "--accept-ambiguous",
        action="store_true",
        help="explicitly accept an ambiguous encoding detection",
    )
    normalize.add_argument(
        "--max-output-bytes", type=_positive_integer, default=256 * 1024 * 1024, metavar="N"
    )
    return parser


def _common_arguments(inspect: argparse.ArgumentParser) -> None:
    inspect.add_argument("path", metavar="PATH")
    inspect.add_argument("--format", choices=("json", "text"), default="text")
    inspect.add_argument("--encoding", metavar="NAME", help="override encoding detection")
    inspect.add_argument(
        "--delimiter", type=_delimiter, metavar="CHAR", help=r"override; accepts \t"
    )
    inspect.add_argument("--no-header", action="store_true", help="treat the first record as data")
    inspect.add_argument("--contract", metavar="PATH", help="local JSON import contract")
    inspect.add_argument(
        "--max-bytes",
        type=_positive_integer,
        default=64 * 1024 * 1024,
        metavar="N",
        help="maximum input snapshot size in bytes (default: 67108864)",
    )
    inspect.add_argument(
        "--max-issues",
        type=_positive_integer,
        default=100,
        metavar="N",
        help="maximum retained issue details; counts continue (default: 100)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Return 0 for acceptable input, 1 for a failed report, or 2 for usage/I/O errors."""
    args = _parser().parse_args(argv)
    try:
        contract = load_contract(args.contract) if args.contract else None
        if args.command == "inspect":
            report = inspect_file(
                args.path,
                encoding=args.encoding,
                delimiter=args.delimiter,
                header=False if args.no_header else None,
                contract=contract,
                max_bytes=args.max_bytes,
                max_issues=args.max_issues,
            )
            accepted = report.ok and not (args.fail_on_warning and report.warning_count)
            payload = report.to_dict()
            payload["policy_passed"] = bool(accepted)
            text_report = _text_report(report)
            if args.fail_on_warning and report.warning_count:
                text_report += "\nPolicy: FAILED (--fail-on-warning)."
            status = 0 if accepted else 1
        else:
            result = normalize_file(
                args.path,
                args.output,
                encoding=args.encoding,
                delimiter=args.delimiter,
                header=False if args.no_header else None,
                contract=contract,
                max_bytes=args.max_bytes,
                max_issues=args.max_issues,
                accept_ambiguous=args.accept_ambiguous,
                max_output_bytes=args.max_output_bytes,
            )
            payload = result.to_dict()
            text_report = _text_report(result.report) + (
                f"\nUTF-8 output: {_safe(result.output)}"
                f"\nOutput bytes: {result.output_bytes}"
                f"\nOutput SHA-256: {result.output_sha256}"
            )
            status = 0
    except InspectionFailed as exc:
        payload = {"ok": False, "error": str(exc), "inspection": exc.report.to_dict()}
        text_report = _text_report(exc.report) + f"\nNormalization refused: {_safe(str(exc))}"
        status = 1
    except FileExistsError:
        print("ingest-sentry: output already exists; no file was overwritten.", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ingest-sentry: input, contract, or output directory not found.", file=sys.stderr)
        return 2
    except PermissionError:
        print(
            "ingest-sentry: permission denied reading input or creating output/snapshot.",
            file=sys.stderr,
        )
        return 2
    except IsADirectoryError:
        print("ingest-sentry: input must be a regular file.", file=sys.stderr)
        return 2
    except OSError:
        print("ingest-sentry: cannot read input or create output/snapshot.", file=sys.stderr)
        return 2
    except (ValueError, LookupError):
        print(
            "ingest-sentry: invalid options or contract; check encoding, delimiter, and limits.",
            file=sys.stderr,
        )
        return 2
    output = (
        json.dumps(payload, ensure_ascii=True, indent=2) if args.format == "json" else text_report
    )
    try:
        print(output)
    except (BrokenPipeError, OSError):
        return 2
    return status
