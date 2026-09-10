"""Versioned, content-redacted inspection reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Literal["error", "warning"]
    message: str
    record: int | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class EncodingInfo:
    name: str | None
    origin: Literal["detected", "provided"]
    status: str
    confidence: float | None
    bytes_validated: int
    complete: bool


@dataclass(frozen=True, slots=True)
class CsvDialect:
    delimiter: str
    header: bool
    inferred: bool
    quotechar: str = '"'
    doublequote: bool = True


@dataclass(frozen=True, slots=True)
class InspectionReport:
    source: str
    format: Literal["csv", "tsv"]
    byte_count: int
    sha256: str | None
    encoding: EncodingInfo
    dialect: CsvDialect | None
    records: int
    physical_lines: int
    line_endings: dict[str, int]
    columns: int | None
    complete: bool
    issues: tuple[Issue, ...]
    issue_count: int
    error_count: int
    warning_count: int
    issues_truncated: bool
    detector_version: str
    schema_version: str = "1"

    @property
    def ok(self) -> bool:
        """No structural errors and the parser reached EOF; not proof of intent."""
        return self.complete and self.error_count == 0

    @property
    def suggested_read_csv_kwargs(self) -> dict[str, Any] | None:
        """Suggested pandas parameters, never an encoding correctness guarantee."""
        if not self.ok or self.dialect is None or self.encoding.name is None:
            return None
        return {
            "encoding": self.encoding.name,
            "encoding_errors": "strict",
            "sep": self.dialect.delimiter,
            "header": 0 if self.dialect.header else None,
            "quotechar": self.dialect.quotechar,
            "doublequote": self.dialect.doublequote,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["ok"] = self.ok
        result["suggested_read_csv_kwargs"] = self.suggested_read_csv_kwargs
        return result
