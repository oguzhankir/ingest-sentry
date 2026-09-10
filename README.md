# ingest-sentry

Inspect CSV and TSV files **before an import fails**. An offline Python API and CLI
that combine [bytesense](https://github.com/oguzhankir/bytesense) encoding detection
with strict decoding and CSV structure checks. Input files are never modified.

**Status:** first inspection milestone, version 0.1.0. Install from this repository;
no PyPI release is required by these instructions. This is not a general data-cleaning
platform, and a successful inspection does not establish business-level correctness.

## Try it

Python 3.10 or later:

```bash
git clone https://github.com/oguzhankir/ingest-sentry.git
cd ingest-sentry
python -m pip install .

ingest-sentry inspect examples/vendor_export.csv
ingest-sentry inspect examples/vendor_export.csv --format json
ingest-sentry inspect examples/vendor_export_broken.csv --encoding utf-8 --delimiter ';'
```

Cloning requires repository access while the repository is private. The equivalent
module entry point is `python -m ingest_sentry`; `ingest-sentry --version` shows the
installed version.

The bundled synthetic export includes a quoted multiline field. The broken variant
adds a record with a missing field; the final command exits `1` and reports:

```text
Data records: 3; columns: 3; physical lines: 5
Line endings: LF=5, CRLF=0, CR=0
Issues: 1 (1 errors, 0 warnings)
  ERROR column_count; record 4; physical lines 5-5: Expected 3 columns; found 2.
```

These are the final lines of the report, not the CSV's contents. The clean variant
exits `0`. For your own exports, override encoding with `--encoding cp1254` when that
is the known format; use `--delimiter '\t' --no-header` for headerless tab-separated data.

## What it checks

- Encoding detection through bytesense, or an explicit encoding override.
- Strict decoding of the complete snapshot, not just a prefix.
- CSV/TSV delimiter inference, or an explicit single-character delimiter.
- Header-aware field-count consistency, malformed CSV, and physical line locations.
- Content-redacted text or versioned JSON reports with an input SHA-256 and issue counts.

`--no-header` makes the first parsed record a data record. Delimiter inference is
heuristic and considers comma, semicolon, tab, and pipe; `.tsv` files default to tab.
Specify `--delimiter` when the format is known. Quoted fields use double quotes and
doubled-quote escaping. CSV syntax checks follow Python's `csv.reader(strict=True)`;
they are not a separate RFC-conformance validator. An encoding that decodes
all bytes can still produce the wrong text, especially for single-byte encodings.
The detector score is not a calibrated probability. Encoding origin, strict decoding,
and structural success are reported separately.

### Python API

```python
from ingest_sentry import inspect_file

report = inspect_file("examples/vendor_export.csv", encoding="utf-8", delimiter=";")
print(report.ok)
print(report.to_dict())

if report.ok:
    # Suggestions only: pandas is not a dependency or an installed adapter.
    print(report.suggested_read_csv_kwargs)
```

`inspect_file(path, *, encoding=None, delimiter=None, header=True,
max_bytes=64 * 1024 * 1024, max_issues=100)` returns an `InspectionReport`.
Reports include `schema_version`, `encoding`, `dialect`, data `records`,
`physical_lines`, `line_endings` (LF/CRLF/CR), `columns`, `complete`, aggregate counts,
and bounded issue details.
Issue locations identify logical records and physical line spans separately, so
quoted multiline records are not mistaken for several data records.
Locations are 1-based; issue record numbers include the header, while `records`
counts data records only. Blank physical lines outside quoted records are skipped.

`ok` requires a complete parse and no errors; warnings do not fail the inspection.
When a limit prevents completion, partial counts are not totals. An incomplete input
snapshot has no full-file SHA-256. Invalid options and input I/O errors raise exceptions
in the Python API; the CLI returns a concise diagnostic without exception contents.

| CLI exit code | Meaning |
| --- | --- |
| `0` | Complete inspection with no errors; warnings may exist |
| `1` | Inspection found errors or could not complete within its limits |
| `2` | Invalid arguments, invalid options, or input/output failure |

## Privacy and resource limits

Inspection makes no network requests and has no telemetry. Reports omit cell values
and header strings, but retain filename/hash metadata and structural information.
Review that metadata before sharing a report. JSON escapes non-ASCII characters and
terminal controls; text output escapes untrusted filenames.

A temporary snapshot lets encoding validation and parsing inspect the same captured
bytes. The default maximum input size is **64 MiB** (`--max-bytes`); the snapshot
spools to temporary disk after **1 MiB** of retained buffering and is closed after
inspection. Temporary files can contain the input data: use an appropriately secured
temporary directory for sensitive exports. This is **not a total-process RSS limit**;
decoder state, parsed records, and runtime overhead also use memory.

`--max-issues` bounds retained issue details (default 100); aggregate counts continue
while parsing can continue. Physical lines are limited to 1,048,576 decoded characters,
including their line ending. The standard-library CSV field-size limit also applies;
exceeding it stops parsing with a diagnostic. Both CLI size/detail limits must be positive.
On Python 3.10, the CSV parser also stops at NUL characters; they remain reported as
control-character errors and the report explicitly marks parsing incomplete.
A regular-file snapshot does not lock an actively changing source: inspect a stable
export for reproducible results.

## Scope

This milestone ships CSV/TSV inspection, a typed Python API, and a CLI. Automatic repair,
schema contracts, JSONL, pandas/Polars adapters, and a reusable GitHub Action are **not
implemented**. No upload service or web application is included.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
ruff format --check .
mypy src/ingest_sentry
python -m build
python scripts/check_dist.py dist
python -m twine check --strict dist/*
```

CI exercises installed packages on Linux, macOS, and Windows, including bytesense's
pure-Python and native backends. It builds and validates wheel/source distributions;
it does not publish packages or releases.

MIT licensed. Powered by [bytesense](https://github.com/oguzhankir/bytesense).
