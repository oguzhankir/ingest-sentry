"""Optional, string-preserving DataFrame imports from validated captured bytes."""

from __future__ import annotations

import importlib
import os
from typing import Any, Literal

from . import inspection
from .contracts import ImportContract
from .errors import InspectionFailed, require_accepted


def _read_frame(
    engine: Literal["pandas", "polars"],
    path: str | os.PathLike[str],
    *,
    encoding: str | None,
    delimiter: str | None,
    header: bool | None,
    contract: ImportContract | None,
    max_bytes: int,
    max_issues: int,
    accept_ambiguous: bool,
) -> Any:
    with inspection._inspection_session(
        path,
        encoding=encoding,
        delimiter=delimiter,
        header=header,
        contract=contract,
        max_bytes=max_bytes,
        max_issues=max_issues,
    ) as (report, snapshot):
        require_accepted(report, accept_ambiguous)
        if report.dialect is None or report.columns is None:
            raise InspectionFailed(report, "Input contains no validated tabular records.")
        rows = list(inspection._iter_rows(snapshot, report))
        if not rows:
            raise InspectionFailed(report, "Input contains no validated tabular records.")
        if report.dialect.header:
            names = rows.pop(0)
            # Inspection only warns about these headers, and its issue list may be
            # truncated. Check captured values, not retained warning messages.
            if any(not name.strip() for name in names) or len(set(names)) != len(names):
                raise InspectionFailed(
                    report, "DataFrame imports require nonblank, unique column names."
                )
        else:
            names = [f"column_{index + 1}" for index in range(report.columns)]
        if any(len(row) != len(names) for row in rows):
            raise InspectionFailed(report, "Captured records have inconsistent column counts.")

        try:
            backend = importlib.import_module(engine)
        except ImportError:
            raise ImportError(
                f"The {engine} adapter requires the optional dependency; "
                f"run pip install 'ingest-sentry[{engine}]'."
            ) from None

        # No backend parser gets a path, bytes, or a chance to infer CSV syntax.
        # Keep the exception boundary around materialization only: backend errors
        # can include cell contents, whereas input and I/O errors must propagate.
        try:
            if engine == "pandas":
                return backend.DataFrame(rows, columns=names, dtype="string")
            return backend.DataFrame(
                rows,
                schema=[(name, backend.String) for name in names],
                orient="row",
                strict=True,
            )
        except Exception:
            raise InspectionFailed(
                report, f"Validated data could not be materialized by {engine}."
            ) from None


def read_pandas(
    path: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    header: bool | None = None,
    contract: ImportContract | None = None,
    max_bytes: int = 64 * 1024 * 1024,
    max_issues: int = 100,
    accept_ambiguous: bool = False,
) -> Any:
    """Return a pandas DataFrame with every cell preserved as a string.

    Requires ``ingest-sentry[pandas]``. Inspection and import use one captured
    input; changes to the source after capture cannot change the imported rows.
    Empty strings and literal NA/null values remain strings, and contracts only
    validate values: they never cast columns. Blank or duplicate header names
    are rejected. Without a header, names are ``column_1``, ``column_2``, etc.

    This is an eager import: the parsed Python rows and the complete DataFrame
    are materialized in memory. ``max_bytes`` limits the captured input, not
    DataFrame size or total process memory. Optional pandas types are not needed
    to import or type-check the core package.
    """
    return _read_frame(
        "pandas",
        path,
        encoding=encoding,
        delimiter=delimiter,
        header=header,
        contract=contract,
        max_bytes=max_bytes,
        max_issues=max_issues,
        accept_ambiguous=accept_ambiguous,
    )


def read_polars(
    path: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    header: bool | None = None,
    contract: ImportContract | None = None,
    max_bytes: int = 64 * 1024 * 1024,
    max_issues: int = 100,
    accept_ambiguous: bool = False,
) -> Any:
    """Return an eager Polars DataFrame with an explicit String column schema.

    Requires ``ingest-sentry[polars]``. The same captured bytes are inspected
    and imported; no backend CSV parser or dtype inference is used. Empty
    strings, literal NA/null values, leading zeros, and exact decimals remain
    strings. Contracts validate without casting. Blank or duplicate header
    names are rejected; headerless imports use ``column_1``, ``column_2``, etc.

    All parsed Python rows and the complete DataFrame are materialized in
    memory. ``max_bytes`` limits captured input bytes, not DataFrame size or
    total process memory. The result is not a lazy or streaming DataFrame.
    """
    return _read_frame(
        "polars",
        path,
        encoding=encoding,
        delimiter=delimiter,
        header=header,
        contract=contract,
        max_bytes=max_bytes,
        max_issues=max_issues,
        accept_ambiguous=accept_ambiguous,
    )
