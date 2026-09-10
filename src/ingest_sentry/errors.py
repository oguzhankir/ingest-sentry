"""Content-redacted failures for operations that require validated input."""

from __future__ import annotations

from .models import InspectionReport


class InspectionFailed(Exception):
    """An import or normalization was rejected; the original report is available."""

    def __init__(
        self,
        report: InspectionReport,
        message: str = "Input did not pass import validation.",
    ) -> None:
        super().__init__(message)
        self.report = report


def require_accepted(report: InspectionReport, accept_ambiguous: bool = False) -> None:
    """Apply strict consumer policy without redefining diagnostic inspection.ok."""
    if type(accept_ambiguous) is not bool:
        raise ValueError("accept_ambiguous must be a boolean.")
    if not report.ok:
        raise InspectionFailed(report)
    if report.encoding.status == "ambiguous" and not accept_ambiguous:
        raise InspectionFailed(
            report,
            "Ambiguous encoding requires an explicit encoding or accept_ambiguous=True.",
        )
