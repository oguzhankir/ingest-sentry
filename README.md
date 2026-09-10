# ingest-sentry

Catch broken CSV and TSV imports **before they reach your DataFrame or pipeline**.
Powered by [bytesense](https://github.com/oguzhankir/bytesense): encoding detection,
full-snapshot validation, import contracts, safe UTF-8 conversion, and a GitHub Action.
Inspection is offline. Source files are never modified.

**Status:** pre-release 0.1.0. Install from this repository; no PyPI release is required
by these instructions. This is not a general data-cleaning platform, and a successful
inspection does not establish business-level correctness.

## Try it

Python 3.10 or later:

```bash
git clone https://github.com/oguzhankir/ingest-sentry.git
cd ingest-sentry
python -m pip install .

ingest-sentry inspect examples/vendor_export.csv
ingest-sentry inspect examples/vendor_export.csv --format json
ingest-sentry inspect examples/vendor_export.csv --contract examples/vendor.contract.json
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
- Optional contracts: exact header order, column types/nullability, and record bounds.
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

print(report.records, report.error_count)
```

`inspect_file(path, *, encoding=None, delimiter=None, header=None, contract=None,
max_bytes=64 * 1024 * 1024, max_issues=100)` returns an `InspectionReport`.
`header=None` takes the contract setting, or defaults to a header when no contract exists.
Reports include `schema_version`, `encoding`, `dialect`, data `records`,
`physical_lines`, `line_endings` (LF/CRLF/CR), `columns`, `complete`, aggregate counts,
and bounded issue details.
Issue locations identify logical records and physical line spans separately, so
quoted multiline records are not mistaken for several data records.
Locations are 1-based; issue record numbers include the header, while `records`
counts data records only. Blank physical lines outside quoted records are skipped.

`ok` requires a complete parse and no errors; warnings do not fail ordinary inspection.
The CLI's `--fail-on-warning` makes warnings fail the command; JSON `policy_passed`
records this decision separately from structural `ok`.
When a limit prevents completion, partial counts are not totals. An incomplete input
snapshot has no full-file SHA-256. Invalid options and input I/O errors raise exceptions
in the Python API; the CLI returns a concise diagnostic without exception contents.

| CLI exit code | Meaning |
| --- | --- |
| `0` | Inspection policy passed, or UTF-8 output was successfully created |
| `1` | Validation/policy failure, incomplete inspection, or refused normalization |
| `2` | Invalid arguments, invalid options, or input/output failure |

### Enforce an import contract

The bundled `examples/vendor.contract.json` describes the synthetic vendor export:

```json
{
  "version": 1,
  "format": "csv",
  "encoding": "utf-8",
  "delimiter": ";",
  "header": true,
  "min_records": 1,
  "columns": [
    {"name": "id", "type": "integer"},
    {"name": "description", "type": "string"},
    {"name": "amount", "type": "number"}
  ]
}
```

```python
from ingest_sentry import inspect_file, load_contract

contract = load_contract("examples/vendor.contract.json")
report = inspect_file("examples/vendor_export.csv", contract=contract)
assert report.ok
```

Contracts validate every parsed data record; column names and order match exactly.
Types are `string`, signed ASCII `integer`, finite decimal `number` (including
exponents), lowercase `true`/`false` `boolean`, and exact `YYYY-MM-DD` `date`.
An empty string is null and requires `"nullable": true`; whitespace is not trimmed.
Numbers are checked lexically, not converted to machine floats: very large decimal
exponents can be valid. Optional `max_records` is inclusive; bounds count data records,
excluding the header. Contract options are authoritative: conflicting explicit encoding,
delimiter, or header options raise an error. Unknown keys, duplicate JSON keys and
unsupported versions are rejected; contract files are limited to 64 KiB/1,024 columns.

### Create a new UTF-8 file safely

```bash
ingest-sentry normalize examples/vendor_export.csv --contract examples/vendor.contract.json --output vendor.utf8.csv
```

```python
from ingest_sentry import normalize_file

result = normalize_file("legacy.csv", "legacy.utf8.csv", encoding="cp1254", delimiter=";")
print(result.output_sha256)
```

Validation and conversion use the same captured bytes. Conversion preserves decoded
text, delimiters, quotes, and newline sequences; BOM handling follows the selected
source codec. It never repairs malformed rows, guesses missing fields, or replaces
undecodable characters. `fix` is an alias for `normalize`, **not** a data-repair command.

The destination must not exist and its parent directory must exist. A private temporary
file is written and synced, then published using an atomic no-overwrite hard link.
Existing files, including symlinks and a destination created by another writer, are
never overwritten. Filesystems without hard-link support fail safely. Default output
limit: 256 MiB (`--max-output-bytes`). Normal failures clean up temporary output;
abrupt process termination can leave a private temporary file, not a partial destination.
A cleanup error after publication can leave a complete destination and its temporary
hard link alongside an I/O error; inspect that destination before retrying.

Normalization and adapters reject ambiguous detection by default, even when inspection
has only a warning. Supply a known encoding, or consciously opt in with
`--accept-ambiguous` / `accept_ambiguous=True`. Validation refusals raise
`InspectionFailed`, whose `report` contains content-redacted diagnostics.

### Import into pandas or Polars

From a checkout:

```bash
python -m pip install '.[pandas,polars]'
```

```python
from ingest_sentry import load_contract, read_pandas, read_polars

contract = load_contract("examples/vendor.contract.json")
df = read_pandas("examples/vendor_export.csv", contract=contract)
pl_df = read_polars("examples/vendor_export.csv", contract=contract)
```

Both adapters build frames from the validated snapshot, without reopening the source
or handing it to a second CSV parser. All columns remain strings: leading zeros,
decimal precision, empty strings, and literal `NA`/`null` are preserved. Contracts
validate but never cast; perform explicit domain-specific casts afterward.
Blank/duplicate headers are refused. Headerless columns are named `column_1`, etc.
These are eager adapters, not streaming readers: parsed rows and the DataFrame use
additional memory, and `max_bytes` is not a DataFrame-size or process-memory limit.
Neither optional dependency is imported by the core package until its adapter runs.

### Gate imports in GitHub Actions

Inside this repository, after checking out the code:

```yaml
permissions:
  contents: read
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./
        id: imports
        with:
          path: examples/vendor_export.csv
          contract: examples/vendor.contract.json
          fail-on-warning: 'true'
```

After merging, authorized consumer repositories can use
`oguzhankir/ingest-sentry@<full-tested-commit-sha>` instead of `./`; replace the placeholder
with a real reviewed SHA. No release tag is assumed. Private-repository Action sharing
must be permitted by the repository owner; the action does not change those settings.

Inputs support `path`, `contract`, `encoding`, `delimiter`, `no-header`, `max-bytes`,
`max-issues`, and `fail-on-warning`. File paths must be relative to the checked-out
workspace and cannot escape it. Setup uses Python 3.12 and a temporary isolated
environment; dependency installation needs network access. Inspection itself is offline.
Outputs are `ok` (including warning policy), `error-count`, `warning-count`, and
`report-path`. The redacted JSON remains in a private randomized runner temporary
directory, including on validation failure; it is not automatically uploaded.

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

CSV/TSV inspection, contracts, UTF-8 normalization, optional pandas/Polars adapters,
and a reusable GitHub Action are implemented. JSONL, automatic mojibake/row repair,
schema inference, spreadsheet-formula sanitization, an upload service, and a web
application are not included. This validates import structure and explicit contracts;
it is not a security sandbox or a replacement for domain-specific data quality rules.

## Development

```bash
python -m pip install -e '.[dev,pandas,polars]'
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
