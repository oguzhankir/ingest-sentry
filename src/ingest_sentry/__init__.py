"""Offline CSV/TSV import diagnostics powered by bytesense."""

from .inspection import inspect_file
from .models import CsvDialect, EncodingInfo, InspectionReport, Issue

__version__ = "0.1.0"

__all__ = ["CsvDialect", "EncodingInfo", "InspectionReport", "Issue", "inspect_file"]
