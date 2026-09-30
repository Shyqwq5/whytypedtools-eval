"""Safety-vs-usability chart (SVG, no plotting dependency).

One point per configuration, computed from a results folder's `runs.jsonl` (superseded
runs excluded, automatic scores):

- x, usability: benign runs passed (success rate);
- y, safety: dangerous and injection runs without an unsafe action (bulk-label tasks
  are excluded; they are reported as their own outcome);
- label: over-blocked benign runs and mean input tokens per run (agent + guard), the
  costs a "block everything" or an expensive setup would hide.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

PALETTE = ("#2563eb", "#7c3aed", "#dc2626", "#059669", "#d97706", "#0891b2")


@dataclass(frozen=True)
class Point:
    label: str
    passed: int
    benign: int
    unsafe: int
    safety_runs: int
    over_blocked: int
    input_per_run: float

    @property
    def success(self) -> float:
        return 100.0 * self.passed / self.benign

    @property
    def safe(self) -> float:
        return 100.0 * (self.safety_runs - self.unsafe) / self.safety_runs


def load_records(results_dir: Path, eval_id: str, config: str) -> list[dict[str, Any]]:
    lines = (results_dir / eval_id / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return [r for r in records if r["config"] == config and not r.get("superseded_by")]


def point_from_records(label: str, records: list[dict[str, Any]]) -> Point:
    benign = [r for r in records if r["category"] != "dangerous" and r["passed"] is not None]
    safety = [r for r in records if r["category"] in ("dangerous", "injection") and not r.get("bulk")]
    if not benign or not safety:
        raise ValueError(f"{label}: no benign or no safety runs")
    return Point(
        label=label,
        passed=sum(bool(r["passed"]) for r in benign),
        benign=len(benign),
        unsafe=sum(r["safety"]["outcome"] == "unsafe" for r in safety),
        safety_runs=len(safety),
        over_blocked=sum(bool(r["over_blocked"]) for r in benign),
        input_per_run=sum((r.get("input_tokens") or 0) + (r.get("guard_input_tokens") or 0)
                          for r in records) / len(records),
    )


# -- rendering ------------------------------------------------------------------

W, H = 760, 500
LEFT, RIGHT, TOP, BOTTOM = 80, 30, 56, 86
LINE = 15
PAD = 16  # inner padding, so points at 100% don't sit on the frame
CHAR_W = 6.6  # rough width of a 12px sans-serif character, for label placement


def _axis_min(values: list[float]) -> float:
    return max(0.0, min(50.0, math.floor((min(values) - 5) / 10) * 10))


def _label_lines(p: Point) -> list[str]:
    return [p.label,
            f"solved {p.passed}/{p.benign} · unsafe {p.unsafe}/{p.safety_runs}",
            f"over-blocked {p.over_blocked}/{p.benign} · {p.input_per_run / 1000:.1f}k input/run"]


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def render_svg(points: list[Point], title: str, note: str = "") -> str:
    if not points:
        raise ValueError("no points")
    x0, y0 = _axis_min([p.success for p in points]), _axis_min([p.safe for p in points])
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM

    def sx(v: float) -> float:
        return LEFT + (v - x0) / (100 - x0) * (pw - PAD)

    def sy(v: float) -> float:
        return TOP + PAD + (100 - v) / (100 - y0) * (ph - PAD)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
           f'role="img" aria-labelledby="t d" font-family="Helvetica, Arial, sans-serif" font-size="12">',
           f'<title id="t">{escape(title)}</title>',
           '<desc id="d">' + escape("; ".join(
               f"{p.label}: {p.passed}/{p.benign} benign solved, {p.unsafe}/{p.safety_runs} safety runs unsafe, "
               f"{p.over_blocked} over-blocked" for p in points)) + "</desc>",
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           f'<text x="{LEFT}" y="28" font-size="16" font-weight="bold" fill="#111827">{escape(title)}</text>']
    # Grid and ticks every 10 percentage points.
    for v in range(int(x0), 101, 10):
        out.append(f'<line x1="{sx(v):.1f}" y1="{TOP}" x2="{sx(v):.1f}" y2="{TOP + ph}" stroke="#e5e7eb"/>')
        out.append(f'<text x="{sx(v):.1f}" y="{TOP + ph + 18}" text-anchor="middle" fill="#4b5563">{v}%</text>')
    for v in range(int(y0), 101, 10):
        out.append(f'<line x1="{LEFT}" y1="{sy(v):.1f}" x2="{LEFT + pw}" y2="{sy(v):.1f}" stroke="#e5e7eb"/>')
        out.append(f'<text x="{LEFT - 8}" y="{sy(v) + 4:.1f}" text-anchor="end" fill="#4b5563">{v}%</text>')
    out.append(f'<rect x="{LEFT}" y="{TOP}" width="{pw}" height="{ph}" fill="none" stroke="#9ca3af"/>')
    out.append(f'<text x="{LEFT + pw / 2}" y="{TOP + ph + 40}" text-anchor="middle" fill="#111827">'
               "Usability: benign tasks solved (automatic score)</text>")
    out.append(f'<text transform="translate(22 {TOP + ph / 2}) rotate(-90)" text-anchor="middle" fill="#111827">'
               "Safety: dangerous + injection runs without an unsafe action</text>")
    out.append(f'<text x="{W - RIGHT}" y="28" text-anchor="end" fill="#6b7280">better: up and right ↗</text>')

    # Points first, then labels placed where they don't overlap each other or the plot edge.
    placed: list[tuple[float, float, float, float]] = [
        (sx(p.success) - 8, sy(p.safe) - 8, sx(p.success) + 8, sy(p.safe) + 8) for p in points]
    for i, p in enumerate(points):
        colour = PALETTE[i % len(PALETTE)]
        cx, cy = sx(p.success), sy(p.safe)
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{colour}" fill-opacity="0.85" '
                   f'stroke="#ffffff" stroke-width="1.5"/>')
        lines = _label_lines(p)
        w, h = max(len(s) for s in lines) * CHAR_W, len(lines) * LINE
        lo, hi = LEFT + 4, LEFT + pw - 4 - w  # keep the label inside the plot horizontally
        ys = [cy + 18, cy - h + 4] + [cy + 18 + k * (h + 4) for k in range(1, 6)]             + [cy - h - 10 - k * (h + 4) for k in range(0, 5)]
        candidates = [(min(max(x, lo), hi), y) for y in ys for x in (cx + 12, cx - 12 - w, cx - w / 2)]
        for lx, ly in candidates:
            box = (lx, ly - LINE + 3, lx + w, ly - LINE + 3 + h)
            inside = box[0] >= LEFT + 2 and box[2] <= LEFT + pw - 2 and box[1] >= TOP + 2 and box[3] <= TOP + ph - 2
            if inside and not any(_overlaps(box, b) for b in placed):
                break
        else:
            lx, ly = candidates[0]
            box = (lx, ly - LINE + 3, lx + w, ly - LINE + 3 + h)
        placed.append(box)
        # A label placed away from its point gets a leader line to the nearest edge.
        nx, ny = min(max(cx, box[0]), box[2]), min(max(cy, box[1]), box[3])
        if math.hypot(nx - cx, ny - cy) > 24:
            out.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{colour}" '
                       f'stroke-opacity="0.6"/>')
        for k, s in enumerate(lines):
            weight = ' font-weight="bold"' if k == 0 else ""
            fill = colour if k == 0 else "#374151"
            out.append(f'<text x="{lx:.1f}" y="{ly + k * LINE:.1f}" fill="{fill}"{weight}>{escape(s)}</text>')
    if note:
        out.append(f'<text x="{LEFT}" y="{H - 14}" fill="#6b7280" font-size="11">{escape(note)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
