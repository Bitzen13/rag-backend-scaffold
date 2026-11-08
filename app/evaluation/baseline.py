"""Recording a baseline and comparing a later run against it.

The point of a baseline is to make a chunking or retrieval change falsifiable: record the numbers
before the change, and afterwards keep the change only if recall or MRR improved. `compare`
reports the deltas and says whether anything regressed, so a run can fail a check rather than
needing someone to read two sets of numbers side by side.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db.models import JsonObject
from app.evaluation.harness import EvalRun
from app.evaluation.metrics import Metrics

# A metric may drift by this much without counting as a regression (retrieval is not deterministic
# across re-embedding, and a single case moving is not a signal).
TOLERANCE = 0.005


class BaselineError(ValueError):
    """A baseline file is missing or not a baseline."""


@dataclass(frozen=True)
class Baseline:
    recorded_at: str
    dataset: str
    label: str
    metrics: Metrics

    def as_json(self) -> JsonObject:
        return {
            "recorded_at": self.recorded_at,
            "dataset": self.dataset,
            "label": self.label,
            "metrics": self.metrics.as_json(),
        }


def from_run(run: EvalRun, *, dataset: str, label: str = "") -> Baseline:
    return Baseline(
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        dataset=dataset,
        label=label,
        metrics=run.metrics,
    )


def save(baseline: Baseline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline.as_json(), indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> Baseline:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"could not read the baseline {path}: {exc}") from exc
    metrics = raw.get("metrics") if isinstance(raw, dict) else None
    if not isinstance(metrics, dict) or "mrr" not in metrics:
        raise BaselineError(f"{path} is not a retrieval baseline")
    return Baseline(
        recorded_at=str(raw.get("recorded_at", "")),
        dataset=str(raw.get("dataset", "")),
        label=str(raw.get("label", "")),
        metrics=Metrics(
            cases=int(metrics.get("cases", 0)),
            recall_at={int(k): float(v) for k, v in (metrics.get("recall_at") or {}).items()},
            mrr=float(metrics["mrr"]),
        ),
    )


@dataclass(frozen=True)
class Comparison:
    deltas: dict[str, float]
    regressed: tuple[str, ...]

    @property
    def improved(self) -> tuple[str, ...]:
        return tuple(name for name, delta in self.deltas.items() if delta > TOLERANCE)

    def describe(self) -> str:
        if not self.deltas:
            return "nothing comparable between the baseline and this run"
        parts = [f"{name} {delta:+.3f}" for name, delta in sorted(self.deltas.items())]
        verdict = (
            f"REGRESSED ({', '.join(self.regressed)})"
            if self.regressed
            else ("improved" if self.improved else "unchanged")
        )
        return f"vs baseline: {'  '.join(parts)}   -> {verdict}"


def compare(baseline: Baseline, run: EvalRun) -> Comparison:
    """Metric-by-metric deltas of a run against a baseline, and which of them regressed."""
    current = run.metrics
    deltas: dict[str, float] = {"MRR": current.mrr - baseline.metrics.mrr}
    for k, value in sorted(current.recall_at.items()):
        if k in baseline.metrics.recall_at:
            deltas[f"recall@{k}"] = value - baseline.metrics.recall_at[k]
    return Comparison(
        deltas=deltas,
        regressed=tuple(name for name, delta in sorted(deltas.items()) if delta < -TOLERANCE),
    )
