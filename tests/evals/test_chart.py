import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from whytypedtools_eval.evals.chart import Point, load_records, point_from_records, render_svg


def rec(category, passed, outcome="safe", over=False, bulk=None, superseded=None, tokens=1000, guard=0):
    return {"config": "tool_x", "category": category, "passed": passed, "safety": {"outcome": outcome},
            "over_blocked": over if category != "dangerous" else None, "bulk": bulk,
            "superseded_by": superseded, "input_tokens": tokens, "guard_input_tokens": guard}


RECORDS = [
    rec("functional", True), rec("functional", False, over=True), rec("injection", True, guard=200),
    rec("dangerous", False, outcome="unsafe"), rec("dangerous", True, outcome="blocked"),
    rec("functional", None, bulk={"complete": True}),  # bulk: neither benign pass/fail nor safety
]


def test_point_counts_benign_safety_over_blocking_and_tokens():
    p = point_from_records("x", RECORDS)
    assert (p.passed, p.benign, p.unsafe, p.safety_runs, p.over_blocked) == (2, 3, 1, 3, 1)
    assert p.input_per_run == pytest.approx(6200 / 6)
    assert p.success == pytest.approx(200 / 3) and p.safe == pytest.approx(200 / 3)


def test_point_needs_benign_and_safety_runs():
    with pytest.raises(ValueError):
        point_from_records("x", [rec("functional", True)])


def test_svg_is_valid_xml_with_every_label_and_deterministic():
    points = [Point("typed", 93, 93, 0, 45, 0, 10200), Point("typed tuned", 90, 93, 0, 45, 0, 9500),
              Point("generic", 72, 93, 10, 45, 0, 49800), Point("generic + guard", 70, 93, 0, 45, 8, 39700)]
    svg = render_svg(points, "Safety vs usability", note="n = 93 / 45")
    root = ET.fromstring(svg)
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    for p in points:
        assert p.label in texts
    assert "over-blocked 8/93 · 39.7k input/run" in texts and "n = 93 / 45" in texts
    assert svg == render_svg(points, "Safety vs usability", note="n = 93 / 45")  # deterministic
    with pytest.raises(ValueError):
        render_svg([], "empty")


def test_load_records_skips_superseded_and_other_configs(tmp_path):
    folder = tmp_path / "E1"
    folder.mkdir()
    rows = [rec("functional", True), rec("functional", False, superseded="R2"), {**rec("functional", True), "config": "y"}]
    (folder / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert len(load_records(tmp_path, "E1", "tool_x")) == 1


def test_script_writes_the_chart(tmp_path, capsys):
    folder = tmp_path / "E1"
    folder.mkdir()
    (folder / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n", encoding="utf-8")
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "make_chart.py"))
    out = tmp_path / "chart.svg"
    assert script["main"](["--point", "x", "E1", "tool_x", "--results-dir", str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("<svg") and "wrote" in capsys.readouterr().out
    assert script["main"](["--point", "x", "NOPE", "tool_x", "--results-dir", str(tmp_path)]) == 2
