"""Shared validation primitives for the Buy or Wait? data layer.

A :class:`ValidationIssue` always names the offending file, the source row (as
a 1-based line number in the CSV, header included) and the identifier of the
record, so every failure message can be acted on without re-reading the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ValidationIssue:
    """A single, human-actionable dataset or output contract violation."""

    source: str
    message: str
    line: int | None = None
    identifier: str | None = None
    column: str | None = None

    def __str__(self) -> str:
        location = self.source
        if self.line is not None:
            location += f":{self.line}"
        parts = [location]
        if self.identifier:
            parts.append(f"[{self.identifier}]")
        if self.column:
            parts.append(f"column '{self.column}'")
        return f"{' '.join(parts)}: {self.message}"


class DatasetError(Exception):
    """Raised when the dataset or an output file violates the contract."""

    def __init__(self, issues: Iterable[ValidationIssue], summary: str = "") -> None:
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        heading = summary or f"{len(self.issues)} contract violation(s) found"
        detail = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"{heading}:\n{detail}" if detail else heading)


def format_issues(issues: Sequence[ValidationIssue], limit: int | None = None) -> str:
    """Render issues as one indented line each, optionally truncated."""
    shown = list(issues) if limit is None else list(issues[:limit])
    lines = [f"  - {issue}" for issue in shown]
    hidden = len(issues) - len(shown)
    if hidden > 0:
        lines.append(f"  - ... and {hidden} more")
    return "\n".join(lines)


def data_line(row_index: int) -> int:
    """Convert a 0-based data row index to its 1-based CSV line number."""
    return row_index + 2
