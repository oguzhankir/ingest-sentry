"""Strict UTF-8 transcoding with atomic, no-clobber output publication."""

from __future__ import annotations

import codecs
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, BinaryIO, cast

from .contracts import ImportContract
from .errors import InspectionFailed, require_accepted
from .inspection import _inspection_session
from .models import InspectionReport

_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Metadata for a completely written UTF-8 output; no cell values are included."""

    report: InspectionReport
    output: str
    output_bytes: int
    output_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "output": self.output,
            "output_bytes": self.output_bytes,
            "output_sha256": self.output_sha256,
            "inspection": self.report.to_dict(),
        }


def _transcode(
    snapshot: BinaryIO,
    target: BinaryIO,
    report: InspectionReport,
    max_output_bytes: int,
) -> tuple[int, str]:
    assert report.encoding.name is not None
    decoder = codecs.getincrementaldecoder(report.encoding.name)(errors="strict")
    count = 0
    digest = hashlib.sha256()
    snapshot.seek(0)
    try:
        while True:
            chunk = snapshot.read(_CHUNK)
            encoded = decoder.decode(chunk, final=not chunk).encode("utf-8", errors="strict")
            if count + len(encoded) > max_output_bytes:
                raise InspectionFailed(report, "UTF-8 output exceeds max_output_bytes.")
            if encoded:
                written = target.write(encoded)
                if written != len(encoded):
                    raise OSError("UTF-8 output could not be written completely.")
                digest.update(encoded)
                count += len(encoded)
            if not chunk:
                return count, digest.hexdigest()
    except UnicodeError:
        # Encoding exceptions can contain input bytes or entire private cell values.
        raise InspectionFailed(report, "Input cannot be strictly transcoded to UTF-8.") from None


def normalize_file(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    header: bool | None = None,
    contract: ImportContract | None = None,
    max_bytes: int = 64 * 1024 * 1024,
    max_issues: int = 100,
    accept_ambiguous: bool = False,
    max_output_bytes: int = 256 * 1024 * 1024,
) -> NormalizationResult:
    """Validate one snapshot, then transcode it without repairing or reformatting data.

    The source is never written. Decoded text, including delimiters, quoting and
    newline sequences, is preserved exactly. BOM handling follows the selected
    source codec. Ambiguous encoding requires explicit opt-in or a known codec.

    Output must not exist, and its parent must already exist. Publication uses a
    same-directory hard link after writing, flushing and fsyncing a private file.
    Filesystems without hard-link support fail safely; there is no overwrite or
    non-atomic fallback. Process termination can leave a private temporary file,
    but never a partially written published output. A cleanup failure after
    publication raises an IO error, even though the complete output already exists.
    """
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer.")
    if type(accept_ambiguous) is not bool:
        raise ValueError("accept_ambiguous must be a boolean.")
    destination = Path(output).absolute()
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("Output already exists; no file was changed.")

    with _inspection_session(
        source,
        encoding=encoding,
        delimiter=delimiter,
        header=header,
        contract=contract,
        max_bytes=max_bytes,
        max_issues=max_issues,
    ) as (report, snapshot):
        require_accepted(report, accept_ambiguous)
        temporary: Path | None = None
        try:
            # NamedTemporaryFile creates an unpredictable name with private permissions.
            # Close before linking so this also works on Windows.
            with NamedTemporaryFile(
                mode="w+b",
                prefix=".ingest-sentry-",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as target:
                temporary = Path(target.name)
                count, digest = _transcode(
                    snapshot, cast(BinaryIO, target), report, max_output_bytes
                )
                target.flush()
                os.fsync(target.fileno())
            # This is the authoritative existence check: a racing writer also wins
            # without being overwritten. Do not use replace() or rename() here.
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except NotImplementedError:
                raise OSError("Filesystem does not support atomic output publication.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return NormalizationResult(report, destination.name, count, digest)
