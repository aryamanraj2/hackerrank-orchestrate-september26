"""Diagnostic: how the baseline forecast compares to the public examples.

`sample_requests.csv` carries completed output fields for 25 public examples.
They are worth reading as a check on the forecast's own arithmetic, but they
are not labels and nothing here may become one. So this file is a report, not
a test: it has no assertions, no threshold, no pass/fail, and no production
module imports it. Run it by hand to see where the projection and the worked
examples disagree, then go and find the financial reason.

    python3 tests/calibration_report.py

The headroom identity it prints is the one the decision engine will use:
paying X on the request date lowers every later balance by X, so the most that
can be paid is the horizon's lowest balance less the floor the user keeps,
capped at what was asked for.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import REAL_DATASET  # noqa: E402

from cashflow import forecast_for_request  # noqa: E402
from dataset_loader import load_dataset  # noqa: E402

TOLERANCE = Decimal("0.02")


def headroom(forecast, requested: Decimal) -> Decimal:
    """What could be paid on the request date without breaching the floor."""
    room = forecast.minimum_balance_from(forecast.start_date) - forecast.minimum_balance_to_keep
    return max(Decimal(0), min(room, requested))


def report(dataset_root: Path = REAL_DATASET) -> str:
    if not dataset_root.is_dir():
        return f"no dataset at {dataset_root}"
    dataset = load_dataset(dataset_root)
    lines = [
        f"{'request':<12} {'projected':>18} {'example':>18} {'gap':>16}  notes",
        "-" * 96,
    ]
    agreed = 0
    scored = 0
    for sample in dataset.sample_requests:
        if sample.amount_safe_to_pay is None:
            continue
        scored += 1
        request = sample.request
        forecast = forecast_for_request(dataset, request)
        projected = headroom(forecast, request.requested_amount)
        gap = projected - sample.amount_safe_to_pay
        if abs(gap) <= TOLERANCE:
            agreed += 1
        flags = []
        if projected == request.requested_amount:
            flags.append("capped at requested")
        if not forecast.is_complete:
            flags.append(f"{len(forecast.blockers)} blocker(s)")
        held = [note for note in forecast.notes if "withheld" in note.reason]
        unschedulable = [note for note in forecast.notes if note not in held]
        if held:
            flags.append(f"{len(held)} stream(s) withheld on evidence")
        if unschedulable:
            flags.append(f"{len(unschedulable)} stream(s) with no schedule")
        lines.append(
            f"{request.request_id:<12} {projected:>18} {sample.amount_safe_to_pay:>18} "
            f"{gap:>16}  {', '.join(flags)}"
        )
    lines.append("-" * 96)
    lines.append(f"agreeing within {TOLERANCE}: {agreed} of {scored}")
    lines.append(
        "A capped row agrees whenever the projection reaches the requested amount, "
        "so it constrains the forecast only from below."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
