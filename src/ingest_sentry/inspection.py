"""Read-only, bounded-file CSV/TSV inspection over a single captured input."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import os
import stat
import unicodedata
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Literal, cast

import bytesense

from .contracts import ContractValidator, ImportContract
from .models import CsvDialect, EncodingInfo, InspectionReport, Issue

_CHUNK = 65536
_SAMPLE_CHARS = 65536
_MEMORY_LIMIT = 1024 * 1024
_LINE_LIMIT = 1024 * 1024


@dataclass
class _State:
    source: str
    format: Literal["csv", "tsv"]
    max_issues: int
    encoding: EncodingInfo
    byte_count: int = 0
    sha256: str | None = None
    dialect: CsvDialect | None = None
    records: int = 0
    physical_lines: int = 0
    line_endings: dict[str, int] = field(default_factory=lambda: {"LF": 0, "CRLF": 0, "CR": 0})
    columns: int | None = None
    complete: bool = False
    issues: list[Issue] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0

    def emit(self, issue: Issue) -> None:
        self.add(
            issue.code,
            issue.severity,
            issue.message,
            issue.record,
            issue.line_start,
            issue.line_end,
        )

    def add(
        self,
        code: str,
        severity: Literal["error", "warning"],
        message: str,
        record: int | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> None:
        if severity == "error":
            self.error_count += 1
        else:
            self.warning_count += 1
        if len(self.issues) < self.max_issues:
            self.issues.append(Issue(code, severity, message, record, line_start, line_end))

    def report(self) -> InspectionReport:
        count = self.error_count + self.warning_count
        return InspectionReport(
            source=self.source,
            format=self.format,
            byte_count=self.byte_count,
            sha256=self.sha256,
            encoding=self.encoding,
            dialect=self.dialect,
            records=self.records,
            physical_lines=self.physical_lines,
            line_endings=dict(self.line_endings),
            columns=self.columns,
            complete=self.complete,
            issues=tuple(self.issues),
            issue_count=count,
            error_count=self.error_count,
            warning_count=self.warning_count,
            issues_truncated=count > len(self.issues),
            detector_version=bytesense.__version__,
        )


def _validate_options(
    encoding: str | None, delimiter: str | None, header: bool, max_bytes: int, max_issues: int
) -> str | None:
    for name, value in (("max_bytes", max_bytes), ("max_issues", max_issues)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
    if type(header) is not bool:
        raise ValueError("header must be a boolean.")
    if delimiter is not None and (
        not isinstance(delimiter, str)
        or len(delimiter) != 1
        or delimiter in '\r\n\x00"'
        or (not delimiter.isprintable() and delimiter != "\t")
    ):
        raise ValueError("delimiter must be one printable character or a tab, excluding quotes.")
    if encoding is None:
        return None
    if not isinstance(encoding, str) or not encoding.strip():
        raise ValueError("encoding must name a text codec.")
    try:
        codec = codecs.lookup(encoding)
        # TextIOWrapper rejects binary transforms such as base64/zlib.
        with io.TextIOWrapper(io.BytesIO(), encoding=codec.name) as probe:
            probe.read()
        return codec.name
    except (LookupError, TypeError, ValueError) as exc:
        raise ValueError("encoding must name a supported text codec.") from exc


def _capture(path: Path, snapshot: BinaryIO, state: _State, max_bytes: int) -> bool:
    # Nonblocking open lets us reject FIFOs/devices instead of waiting for a writer.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    with ExitStack() as cleanup:
        fd = os.open(path, flags)
        cleanup.callback(os.close, fd)
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError("Input must be a regular file.")
        if not stat.S_ISREG(mode):
            raise ValueError("Input must be a regular file.")
        source = cleanup.enter_context(os.fdopen(fd, "rb", closefd=False))
        digest = hashlib.sha256()
        while chunk := source.read(min(_CHUNK, max_bytes + 1 - state.byte_count)):
            state.byte_count += len(chunk)
            if state.byte_count > max_bytes:
                state.add("file_too_large", "error", "Input exceeds max_bytes; inspection stopped.")
                return False
            digest.update(chunk)
            snapshot.write(chunk)
        state.sha256 = digest.hexdigest()
    snapshot.seek(0)
    return True


def _detect_encoding(snapshot: BinaryIO, state: _State, provided: str | None) -> bool:
    if provided is None:
        result = bytesense.from_fp(snapshot)
        state.encoding = EncodingInfo(
            result.encoding,
            "detected",
            result.status,
            result.confidence,
            result.bytes_validated,
            bool(
                result.encoding and result.complete and result.bytes_validated == state.byte_count
            ),
        )
        if (
            result.encoding is None
            or not result.complete
            or result.bytes_validated != state.byte_count
        ):
            code = "binary_input" if result.status == "binary" else "encoding_unknown"
            state.add(code, "error", "No encoding could be validated for the complete input.")
            return False
        if result.status == "ambiguous":
            state.add(
                "encoding_ambiguous",
                "warning",
                "Multiple encodings are plausible; use a known encoding when available.",
            )
    else:
        try:
            decoder = codecs.getincrementaldecoder(provided)(errors="strict")
            while chunk := snapshot.read(_CHUNK):
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
        except UnicodeError:
            state.encoding = EncodingInfo(provided, "provided", "invalid", None, 0, False)
            state.add(
                "decode_error",
                "error",
                "Input does not strictly decode with the provided encoding.",
            )
            return False
        state.encoding = EncodingInfo(
            provided, "provided", "provided", None, state.byte_count, True
        )
    snapshot.seek(0)
    return True


def _infer_delimiter(sample: str, state: _State) -> str | None:
    candidates: list[tuple[tuple[Fraction, int], str]] = []
    any_records = False
    for delimiter in (",", ";", "\t", "|"):
        widths: list[int] = []
        reader = csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter, strict=True)
        try:
            for row in reader:
                if not row or (len(row) == 1 and not row[0].strip()):
                    continue
                widths.append(len(row))
                if len(widths) >= 100:
                    break
        except csv.Error:
            # An incomplete final quoted record in the bounded sample is not evidence.
            pass
        any_records |= bool(widths)
        multi = Counter(width for width in widths if width > 1)
        if multi:
            count = max(multi.values())
            candidates.append(((Fraction(count, len(widths)), count), delimiter))
    if not candidates:
        if not any_records and sample.strip():
            state.add(
                "dialect_unresolved", "error", "No complete sampled records; provide a delimiter."
            )
            return None
        return ","
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        state.add(
            "dialect_ambiguous", "error", "Multiple delimiters fit the sample; provide a delimiter."
        )
        return None
    return candidates[0][1]


class _LongLineError(Exception):
    pass


class _SnapshotReader(io.BufferedIOBase):
    """Read-only IO facade for Python 3.10 spools, without forcing a disk rollover.

    SpooledTemporaryFile gained the full BufferedIOBase interface in Python 3.11.
    This facade borrows the source; the capture context retains ownership.
    """

    def __init__(self, source: BinaryIO) -> None:
        super().__init__()
        self.source = source

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        return self.source.read(-1 if size is None else size)

    def read1(self, size: int = -1) -> bytes:
        return self.source.read(_CHUNK if size < 0 else size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.source.seek(offset, whence)

    def tell(self) -> int:
        return self.source.tell()


class _Lines:
    def __init__(self, stream: io.TextIOWrapper, state: _State) -> None:
        self.stream = stream
        self.state = state
        self.last = ""

    def __iter__(self) -> _Lines:
        return self

    def __next__(self) -> str:
        line = self.stream.readline(_LINE_LIMIT + 1)
        if not line:
            raise StopIteration
        self.state.physical_lines += 1
        if len(line) > _LINE_LIMIT:
            raise _LongLineError
        self.last = line
        for ending, name in (("\r\n", "CRLF"), ("\r", "CR"), ("\n", "LF")):
            if line.endswith(ending):
                self.state.line_endings[name] += 1
                break
        return line


def _parse(stream: io.TextIOWrapper, state: _State, contract: ImportContract | None = None) -> None:
    assert state.dialect is not None
    validator = ContractValidator(contract, state.emit) if contract is not None else None
    lines = _Lines(stream, state)
    reader = csv.reader(lines, delimiter=state.dialect.delimiter, strict=True)
    logical_record = 0
    start = 1
    try:
        while True:
            start = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                state.complete = True
                break
            if not row or (
                len(row) == 1 and reader.line_num == start and not lines.last.strip(" \t\r\n")
            ):
                continue
            logical_record += 1
            if any(
                unicodedata.category(c) == "Cc" and c not in "\r\n\t" for cell in row for c in cell
            ):
                state.add(
                    "control_character",
                    "error",
                    "Record contains a non-text control character.",
                    logical_record,
                    start,
                    reader.line_num,
                )
            if state.columns is None:
                state.columns = len(row)
                if state.dialect.header:
                    if validator is not None:
                        validator.validate_header(row, logical_record, start, reader.line_num)
                    if any(not name.strip() for name in row):
                        state.add(
                            "blank_header",
                            "warning",
                            "Header contains blank column names.",
                            1,
                            start,
                            reader.line_num,
                        )
                    if len(set(row)) != len(row):
                        state.add(
                            "duplicate_header",
                            "warning",
                            "Header contains duplicate column names.",
                            1,
                            start,
                            reader.line_num,
                        )
                    continue
            state.records += 1
            if validator is not None:
                validator.validate_row(row, logical_record, start, reader.line_num)
            if len(row) != state.columns:
                state.add(
                    "column_count",
                    "error",
                    f"Expected {state.columns} columns; found {len(row)}.",
                    logical_record,
                    start,
                    reader.line_num,
                )
    except csv.Error:
        # Python 3.10 rejects NUL before returning the record for our control scan.
        # Preserve the diagnostic category without pretending parsing reached EOF.
        has_nul = "\x00" in lines.last
        state.add(
            "control_character" if has_nul else "csv_parse_error",
            "error",
            "Record contains a NUL character; CSV parsing stopped."
            if has_nul
            else "Malformed CSV or a field exceeds the Python CSV parser limit.",
            logical_record + 1,
            start,
            max(start, state.physical_lines),
        )
    except _LongLineError:
        state.add(
            "line_too_long",
            "error",
            "A physical line exceeds the 1 Mi-character inspection limit.",
            logical_record + 1,
            start,
            max(start, state.physical_lines),
        )
    except UnicodeError:
        state.add("decode_error", "error", "Decoded text could not be read completely.")
    if state.complete and state.columns is None:
        state.add("empty_file", "error", "Input contains no nonblank CSV records.")
    elif state.complete and state.records == 0:
        state.add("no_data_records", "warning", "Input has a header but no data records.")
    if state.complete and validator is not None:
        validator.finish(state.records)


def inspect_file(
    path: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    header: bool | None = None,
    contract: ImportContract | None = None,
    max_bytes: int = 64 * 1024 * 1024,
    max_issues: int = 100,
) -> InspectionReport:
    """Inspect a local CSV/TSV without rewriting it or disclosing cell contents.

    Encoding validation and CSV parsing refer to the same temporary captured bytes.
    Errors found in complete records do not stop scanning; malformed quoting does.
    An explicit encoding is authoritative, never a hint or replacement-error fallback.
    Limits bound the captured file and retained diagnostics, not total process RSS.
    ``record`` in issues counts nonblank logical records including the header.
    """
    with _inspection_session(
        path,
        encoding=encoding,
        delimiter=delimiter,
        header=header,
        contract=contract,
        max_bytes=max_bytes,
        max_issues=max_issues,
    ) as (report, _snapshot):
        return report


@contextmanager
def _inspection_session(
    path: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    header: bool | None = None,
    contract: ImportContract | None = None,
    max_bytes: int = 64 * 1024 * 1024,
    max_issues: int = 100,
) -> Iterator[tuple[InspectionReport, BinaryIO]]:
    if contract is not None and not isinstance(contract, ImportContract):
        raise ValueError("contract must be an ImportContract.")
    resolved_header = (
        contract.header if header is None and contract else (True if header is None else header)
    )
    encoding = _validate_options(encoding, delimiter, resolved_header, max_bytes, max_issues)
    if contract is not None:
        expected_encoding = (
            codecs.lookup(contract.encoding).name if contract.encoding is not None else None
        )
        if encoding is not None and expected_encoding is not None and encoding != expected_encoding:
            raise ValueError("encoding conflicts with the contract.")
        if (
            delimiter is not None
            and contract.delimiter is not None
            and delimiter != contract.delimiter
        ):
            raise ValueError("delimiter conflicts with the contract.")
        if resolved_header != contract.header:
            raise ValueError("header conflicts with the contract.")
        encoding = encoding or expected_encoding
        delimiter = delimiter if delimiter is not None else contract.delimiter
    source = Path(path)
    kind: Literal["csv", "tsv"] = (
        contract.format if contract else ("tsv" if source.suffix.lower() == ".tsv" else "csv")
    )
    state = _State(
        source.name,
        kind,
        max_issues,
        EncodingInfo(encoding, "provided" if encoding else "detected", "unknown", None, 0, False),
    )
    with SpooledTemporaryFile(max_size=_MEMORY_LIMIT, mode="w+b") as snapshot:
        captured = cast(BinaryIO, snapshot)
        _inspect_captured(
            source, captured, state, encoding, delimiter, resolved_header, max_bytes, contract
        )
        captured.seek(0)
        yield state.report(), captured


def _inspect_captured(
    source: Path,
    snapshot: BinaryIO,
    state: _State,
    encoding: str | None,
    delimiter: str | None,
    header: bool,
    max_bytes: int,
    contract: ImportContract | None,
) -> None:
    if not _capture(source, snapshot, state, max_bytes):
        return
    if state.byte_count == 0:
        state.complete = True
        state.add("empty_file", "error", "Input contains no CSV records.")
        if contract is not None:
            ContractValidator(contract, state.emit).finish(0)
        return
    if not _detect_encoding(snapshot, state, encoding):
        return
    assert state.encoding.name is not None
    stream = io.TextIOWrapper(
        cast(BinaryIO, _SnapshotReader(snapshot)),
        encoding=state.encoding.name,
        errors="strict",
        newline="",
    )
    selected: str | None
    try:
        if delimiter is None:
            if state.format == "tsv":
                selected = "\t"
            else:
                sample = stream.read(_SAMPLE_CHARS + 1)
                if len(sample) > _SAMPLE_CHARS:
                    sample = sample[:_SAMPLE_CHARS]
                    boundary = max(sample.rfind("\n"), sample.rfind("\r"))
                    if boundary < 0:
                        state.add(
                            "dialect_unresolved",
                            "error",
                            "No complete sampled line; provide a delimiter.",
                        )
                        return
                    sample = sample[: boundary + 1]
                selected = _infer_delimiter(sample, state)
            if selected is None:
                return
        else:
            selected = delimiter
        state.dialect = CsvDialect(selected, header, delimiter is None)
        stream.seek(0)
        _parse(stream, state, contract)
    finally:
        stream.detach().close()


def _iter_rows(snapshot: BinaryIO, report: InspectionReport) -> Iterator[list[str]]:
    """Read validated rows, including the header, without reopening the source."""
    assert report.ok and report.encoding.name is not None and report.dialect is not None
    snapshot.seek(0)
    state = _State(report.source, report.format, 1, report.encoding)
    stream = io.TextIOWrapper(
        cast(BinaryIO, _SnapshotReader(snapshot)),
        encoding=report.encoding.name,
        errors="strict",
        newline="",
    )
    try:
        lines = _Lines(stream, state)
        reader = csv.reader(lines, delimiter=report.dialect.delimiter, strict=True)
        while True:
            start = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                return
            if not row or (
                len(row) == 1 and reader.line_num == start and not lines.last.strip(" \t\r\n")
            ):
                continue
            yield row
    finally:
        stream.detach().close()
