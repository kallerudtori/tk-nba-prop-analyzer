"""
Calibration tracking.

The question this answers is narrow and important: when the model says 65%,
does it happen 65% of the time? A model can be well calibrated and
unprofitable, or profitable and badly calibrated, but an uncalibrated model
cannot be staked with Kelly without overbetting, and its confidence numbers
cannot honestly be shown to a user as if they meant something.

Hence `calibration_scale`, which is what the staking layer consumes: it starts
at zero for a model with no graded history and rises only as evidence arrives.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from core.odds_math import brier_score, log_loss

logger = logging.getLogger(__name__)

# Graded predictions required before a model's confidence is considered
# calibrated rather than provisional. Six NFL weeks of picks is the floor named
# in the build spec; below it the UI must say "provisional" and mean it.
PROVISIONAL_THRESHOLD = 40

DEFAULT_BINS = ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


@dataclass
class ReliabilityBin:
    lower: float
    upper: float
    n: int = 0
    predicted_sum: float = 0.0
    observed_sum: int = 0

    @property
    def mean_predicted(self) -> float | None:
        return self.predicted_sum / self.n if self.n else None

    @property
    def observed_rate(self) -> float | None:
        return self.observed_sum / self.n if self.n else None

    def as_dict(self) -> dict:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "n": self.n,
            "mean_predicted": (
                round(self.mean_predicted, 4) if self.mean_predicted is not None else None
            ),
            "observed_rate": (
                round(self.observed_rate, 4) if self.observed_rate is not None else None
            ),
        }


@dataclass
class CalibrationResult:
    n: int = 0
    brier: float | None = None
    log_loss: float | None = None
    calibration_error: float | None = None
    reliability: list = field(default_factory=list)
    is_provisional: bool = True

    def as_dict(self) -> dict:
        return {
            "n": self.n, "brier": self.brier, "log_loss": self.log_loss,
            "calibration_error": self.calibration_error,
            "reliability": self.reliability,
            "is_provisional": self.is_provisional,
        }


def evaluate(
    pairs: list[tuple[float, int]], bins: tuple = DEFAULT_BINS
) -> CalibrationResult:
    """
    Score a set of (predicted_probability, outcome) pairs.

    `calibration_error` is the sample-weighted mean absolute gap between
    predicted and observed rates across bins — a single number for "how far off
    are the stated confidences", which is what the Performance tab leads with.
    """
    usable = [
        (float(p), int(o)) for p, o in pairs
        if p is not None and o is not None and 0.0 <= float(p) <= 1.0
    ]
    if not usable:
        return CalibrationResult()

    briers = [brier_score(p, o) for p, o in usable]
    losses = [log_loss(p, o) for p, o in usable]

    buckets = [ReliabilityBin(lo, hi) for lo, hi in bins]
    for p, o in usable:
        for b in buckets:
            if b.lower <= p < b.upper:
                b.n += 1
                b.predicted_sum += p
                b.observed_sum += o
                break

    weighted_gap = 0.0
    for b in buckets:
        if b.n and b.mean_predicted is not None and b.observed_rate is not None:
            weighted_gap += b.n * abs(b.mean_predicted - b.observed_rate)
    calibration_error = weighted_gap / len(usable) if usable else None

    return CalibrationResult(
        n=len(usable),
        brier=round(sum(briers) / len(briers), 5),
        log_loss=round(sum(losses) / len(losses), 5),
        calibration_error=(
            round(calibration_error, 5) if calibration_error is not None else None
        ),
        reliability=[b.as_dict() for b in buckets],
        is_provisional=len(usable) < PROVISIONAL_THRESHOLD,
    )


def calibration_scale(result: CalibrationResult) -> float:
    """
    Convert calibration quality into the 0..1 multiplier the staking layer
    applies to its Kelly fraction.

    Zero while provisional. That is deliberate and not a placeholder: staking
    Kelly on a model whose probabilities have never been checked is how
    bankrolls die, and the honest unit size for an unproven model is none.
    Above the threshold the scale falls off with calibration error, reaching
    zero at a 10-point average miss.
    """
    if result.is_provisional or result.calibration_error is None:
        return 0.0
    scale = 1.0 - (result.calibration_error / 0.10)
    return round(max(0.0, min(1.0, scale)), 4)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    Confidence interval for a hit rate.

    Displayed next to every hit rate in the Performance tab, because at NFL
    sample sizes the interval is usually wide enough to contain both "sharp"
    and "coin flip", and a bare percentage hides that.
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


def required_sample_size(edge: float, base: float = 0.5, power: float = 0.80) -> int:
    """
    Games needed to distinguish a true hit rate of `base + edge` from `base`.

    Exists to be printed in the Performance tab next to any claim of an edge.
    At the 52.4% break-even point this returns a number in the thousands, which
    is the single most useful thing the tab can tell you about a 12-week record.
    """
    z_alpha, z_beta = 1.96, (0.84 if power <= 0.80 else 1.28)
    if edge <= 0:
        return 0
    variance = base * (1 - base)
    return int(math.ceil(((z_alpha + z_beta) ** 2 * variance) / (edge ** 2)))
