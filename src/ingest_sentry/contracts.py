"""Strict local import contracts and content-redacted CSV/TSV validation."""

from __future__ import annotations

import codecs
import io
import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from os import PathLike
from typing import Any, Literal

from .models import Issue

MAX_CONTRACT_BYTES = 64 * 1024
MAX_CONTRACT_COLUMNS = 1024

ColumnType = Literal["string", "integer", "number", "boolean", "date"]
_COLUMN_TYPES = frozenset({"string", "integer", "number", "boolean", "date"})
_COLUMN_KEYS = frozenset({"name", "type", "nullable"})
_CONTRACT_KEYS = frozenset(
    {
        "columns",
        "encoding",
        "delimiter",
        "header",
        "min_records",
        "max_records",
        "format",
        "version",
    }
)
_INTEGER = re.compile(r"[+-]?[0-9]+")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True, slots=True)
class ColumnRule:
    """A positionally matched column; values are checked without coercion."""

    name: str
    type: ColumnType = "string"
    nullable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Column name must be a nonempty string.")
        if not isinstance(self.type, str) or self.type not in _COLUMN_TYPES:
            raise ValueError("Column type must be string, integer, number, boolean, or date.")
        if type(self.nullable) is not bool:
            raise ValueError("Column nullable must be a boolean.")


def _canonical_text_codec(encoding: str) -> str:
    if not isinstance(encoding, str) or not encoding.strip():
        raise ValueError("Contract encoding must name a supported text codec.")
    try:
        codec = codecs.lookup(encoding)
        # Reject binary transforms and codecs without a usable incremental decoder.
        with io.TextIOWrapper(io.BytesIO(), encoding=codec.name) as probe:
            probe.read()
    except (LookupError, TypeError, ValueError, UnicodeError):
        raise ValueError("Contract encoding must name a supported text codec.") from None
    return codec.name


@dataclass(frozen=True, slots=True)
class ImportContract:
    """An immutable CSV/TSV contract; unknown configuration is never ignored."""

    columns: tuple[ColumnRule, ...]
    encoding: str | None = None
    delimiter: str | None = None
    header: bool = True
    min_records: int = 0
    max_records: int | None = None
    format: Literal["csv", "tsv"] = "csv"
    version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.columns, tuple)
            or not 1 <= len(self.columns) <= MAX_CONTRACT_COLUMNS
            or not all(isinstance(column, ColumnRule) for column in self.columns)
        ):
            raise ValueError("Contract columns must be a tuple of 1 to 1024 ColumnRule objects.")
        if len({column.name for column in self.columns}) != len(self.columns):
            raise ValueError("Contract column names must be unique.")
        if type(self.header) is not bool:
            raise ValueError("Contract header must be a boolean.")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Contract version must be integer 1.")
        if not isinstance(self.format, str) or self.format not in {"csv", "tsv"}:
            raise ValueError("Contract format must be csv or tsv.")
        if type(self.min_records) is not int or self.min_records < 0:
            raise ValueError("Contract min_records must be a nonnegative integer.")
        if self.max_records is not None and (
            type(self.max_records) is not int or self.max_records < self.min_records
        ):
            raise ValueError("Contract max_records must be an integer not less than min_records.")
        if self.delimiter is not None and (
            not isinstance(self.delimiter, str)
            or len(self.delimiter) != 1
            or self.delimiter in '\r\n\x00"'
            or (not self.delimiter.isprintable() and self.delimiter != "\t")
        ):
            raise ValueError(
                "Contract delimiter must be one printable character or tab, not a quote."
            )
        if self.encoding is not None:
            object.__setattr__(self, "encoding", _canonical_text_codec(self.encoding))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate contract key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON numbers are not supported.")


def load_contract(path: str | PathLike[str]) -> ImportContract:
    """Load at most 64 KiB of strict UTF-8 JSON from a local file.

    Configuration errors do not echo keys, values, or paths. Filesystem errors retain
    their original OSError type; callers can redact the path when presenting them.
    No includes, external references, or remote retrieval are supported.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    with ExitStack() as cleanup:
        fd = os.open(path, flags)
        cleanup.callback(os.close, fd)
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError("Contract input must be a regular file.")
        if not stat.S_ISREG(mode):
            raise ValueError("Contract input must be a regular file.")
        stream = cleanup.enter_context(os.fdopen(fd, "rb", closefd=False))
        raw = stream.read(MAX_CONTRACT_BYTES + 1)
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ValueError("Contract file exceeds the 64 KiB limit.")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise ValueError("Contract must contain valid UTF-8 JSON with unique keys.") from None
    if not isinstance(data, dict) or data.keys() - _CONTRACT_KEYS:
        raise ValueError("Contract must be an object containing only supported fields.")
    columns = data.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_CONTRACT_COLUMNS:
        raise ValueError("Contract columns must be an array containing 1 to 1024 objects.")
    rules = []
    for column in columns:
        if not isinstance(column, dict) or column.keys() - _COLUMN_KEYS or "name" not in column:
            raise ValueError("Each contract column must contain a name and only supported fields.")
        rules.append(ColumnRule(**column))
    data["columns"] = tuple(rules)
    return ImportContract(**data)


def _matches_type(value: str, column_type: ColumnType) -> bool:
    if column_type == "string":
        return True
    if column_type == "integer":
        return _INTEGER.fullmatch(value) is not None
    if column_type == "number":
        # A finite base-ten literal, not a machine-float conversion: 1e999 is valid.
        return _NUMBER.fullmatch(value) is not None
    if column_type == "boolean":
        return value in {"true", "false"}
    if _DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class ContractValidator:
    """Validate the parser's captured rows without retaining or reporting cell data."""

    def __init__(self, contract: ImportContract, emit: Callable[[Issue], None]) -> None:
        self.contract = contract
        self._emit = emit

    def _issue(self, code: str, message: str, record: int, line_start: int, line_end: int) -> None:
        self._emit(Issue(code, "error", message, record, line_start, line_end))

    def _valid_width(self, row: list[str], record: int, line_start: int, line_end: int) -> bool:
        if len(row) == len(self.contract.columns):
            return True
        self._issue(
            "contract_column_count",
            "Record column count does not match the contract.",
            record,
            line_start,
            line_end,
        )
        return False

    def validate_header(self, row: list[str], record: int, line_start: int, line_end: int) -> None:
        """Check exact header names and order without including them in diagnostics."""
        if not self._valid_width(row, record, line_start, line_end):
            return
        if any(
            value != column.name for value, column in zip(row, self.contract.columns, strict=True)
        ):
            self._issue(
                "contract_header",
                "Header names or order do not match the contract.",
                record,
                line_start,
                line_end,
            )

    def validate_row(self, row: list[str], record: int, line_start: int, line_end: int) -> None:
        """Check each cell at its one-based position; never strip or coerce values."""
        if not self._valid_width(row, record, line_start, line_end):
            return
        for position, (value, column) in enumerate(
            zip(row, self.contract.columns, strict=True), start=1
        ):
            if value == "":
                if not column.nullable:
                    self._issue(
                        "contract_null",
                        f"Column {position} does not permit an empty value.",
                        record,
                        line_start,
                        line_end,
                    )
            elif not _matches_type(value, column.type):
                self._issue(
                    "contract_type",
                    f"Column {position} does not match its declared type.",
                    record,
                    line_start,
                    line_end,
                )

    def finish(self, records: int) -> None:
        """Check data-record bounds after parsing, excluding the header."""
        if records < self.contract.min_records:
            self._emit(
                Issue(
                    "contract_min_records",
                    "error",
                    "Data-record count is below the contract minimum.",
                )
            )
        if self.contract.max_records is not None and records > self.contract.max_records:
            self._emit(
                Issue(
                    "contract_max_records",
                    "error",
                    "Data-record count exceeds the contract maximum.",
                )
            )
