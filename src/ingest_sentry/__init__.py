"""Offline CSV/TSV import diagnostics powered by bytesense."""

from .adapters import read_pandas, read_polars
from .contracts import ColumnRule, ImportContract, load_contract
from .errors import InspectionFailed
from .inspection import inspect_file
from .models import CsvDialect, EncodingInfo, InspectionReport, Issue
from .normalization import NormalizationResult, normalize_file

__version__ = "0.1.0"

__all__ = [
    "ColumnRule",
    "ImportContract",
    "load_contract",
    "CsvDialect",
    "EncodingInfo",
    "InspectionReport",
    "Issue",
    "inspect_file",
    "InspectionFailed",
    "NormalizationResult",
    "normalize_file",
    "read_pandas",
    "read_polars",
]
