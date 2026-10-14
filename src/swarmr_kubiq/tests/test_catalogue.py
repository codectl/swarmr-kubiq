"""Profiling a kubiq checkout and rendering it as prompt facts."""

from __future__ import annotations

from pathlib import Path

from swarmr_kubiq.catalogue import banner, profile_catalogue, render_facts
from swarmr_kubiq.target import activate

DRILL = """\
id: {id}
order: {order}
exam: CKAD
title: {title}
topic: {topic}
task: |
  Do the thing.
solution: |
  kubectl get pods
checks:
  - name: c
    resource: {{ kind: Pod, name: x }}
    assert:
      - {{ path: status.phase, equals: Running }}
"""

EXAM = """\
id: ckad-mock-1
title: CKAD Mock Exam 1
certification: CKAD
description: d
duration_minutes: 120
pass_percent: 75
questions:
  - {{ drill: a-pod, namespace: ckad01-pod, weight: 50 }}
  - {{ drill: a-svc, namespace: ckad02-svc, weight: 50 }}
"""


def _checkout(tmp_path: Path) -> Path:
    (tmp_path / "drills" / "ckad-mock-1").mkdir(parents=True)
    (tmp_path / "exams").mkdir()
    (tmp_path / "drills" / "ckad-mock-1" / "q01-a.yaml").write_text(
        DRILL.format(id="a-pod", order=101, title="A pod", topic="Pods")
    )
    (tmp_path / "drills" / "ckad-mock-1" / "q02-b.yaml").write_text(
        DRILL.format(
            id="a-svc", order=102, title="A service", topic="Services & Networking"
        )
    )
    (tmp_path / "drills" / "ckad-mock-1" / "broken.yaml").write_text("id: [\n")
    (tmp_path / "exams" / "ckad-mock-1.yaml").write_text(EXAM.format())
    return tmp_path


def test_profile_reads_drills_exams_and_reports_broken(tmp_path: Path) -> None:
    t = activate({"kubiq": str(_checkout(tmp_path)), "exam": "ckad-mock-2"})
    cat = profile_catalogue(t)
    assert cat.ids == {"a-pod", "a-svc"}
    assert [e.id for e in cat.exams] == ["ckad-mock-1"]
    assert cat.namespaces() == {"ckad01-pod", "ckad02-svc"}
    assert len(cat.broken) == 1 and "broken.yaml" in cat.broken[0]
    assert cat.by_id("a-svc") is not None and cat.by_id("nope") is None


def test_domain_counts_map_topics_to_curriculum(tmp_path: Path) -> None:
    t = activate({"kubiq": str(_checkout(tmp_path)), "exam": "ckad-mock-2"})
    counts = profile_catalogue(t).domain_counts("CKAD")
    assert counts["Application Design and Build"] == 1
    assert counts["Services and Networking"] == 1


def test_facts_list_every_drill_and_the_target(tmp_path: Path) -> None:
    t = activate({"kubiq": str(_checkout(tmp_path)), "exam": "ckad-mock-2"})
    cat = profile_catalogue(t)
    facts = render_facts(cat, t)
    assert facts.startswith("<facts>") and facts.endswith("</facts>")
    assert "Exam to author: ckad-mock-2 (CKAD, 21 questions)" in facts
    assert "- a-pod · Pods · A pod" in facts
    assert "Services and Networking: 20%" in facts
    assert "broken.yaml" in facts
    assert banner(cat, t).startswith(f"{tmp_path.name}: 2 drills in 1 exams")
