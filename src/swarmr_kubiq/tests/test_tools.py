"""tools: the exam under construction, staged into a kubiq-like tree.

No server, no docs, no model: only the author's and the critic's read/write
tools, which touch the checkout alone. Proving tools are covered by the
harness tests with a fake client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from swarmr_kubiq import tools
from swarmr_kubiq.drill import load_exam
from swarmr_kubiq.target import activate

DRILL = """\
id: {id}
order: 701
exam: CKAD
title: {title}
topic: Pods
task: |
  {task}
solution: |
  kubectl apply -f - <<'EOF'
  apiVersion: v1
  kind: Pod
  metadata:
    name: {name}
  spec:
    containers:
      - name: web
        image: nginx:alpine
  EOF
checks:
  - name: {name} runs nginx
    resource: {{ kind: Pod, name: {name} }}
    assert:
      - {{ path: "spec.containers[0].image", contains: nginx }}
"""


def _drill(
    id: str = "pod-runs-nginx",
    title: str = "Run a Pod",
    task: str = "Create a Pod `web` running `nginx:alpine`.",
    name: str = "web",
) -> str:
    return DRILL.format(id=id, title=title, task=task, name=name)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "drills" / "ckad-mock-1").mkdir(parents=True)
    (tmp_path / "exams").mkdir()
    (tmp_path / "drills" / "ckad-mock-1" / "q01-existing.yaml").write_text(
        _drill(id="existing-pod", title="Existing", name="old")
    )
    (tmp_path / "exams" / "ckad-mock-1.yaml").write_text(
        "id: ckad-mock-1\ntitle: CKAD Mock Exam 1\nquestions:\n"
        "  - { drill: existing-pod, namespace: ckad01-pod, weight: 100 }\n"
    )
    activate({"kubiq": str(tmp_path), "exam": "ckad-mock-7", "questions": "2"})
    tools.reset_staging()
    return tmp_path


def _call(tool: Any, **kwargs: Any) -> dict[str, Any]:
    text = tool.invoke(kwargs)
    assert not text.startswith("tool error"), text
    return json.loads(text)


def _error(tool: Any, **kwargs: Any) -> str:
    text = tool.invoke(kwargs)
    assert text.startswith("tool error"), text
    return text


def test_stage_writes_a_draft_and_rolls_back_a_refused_full_exam(checkout: Path) -> None:
    first = _call(
        tools.stage_drill,
        drill_yaml=_drill(),
        question_index=1,
        namespace="ckad01-pod",
        weight=8,
    )
    assert first["path"] == str(checkout / "drills/ckad-mock-7/q01-pod-runs-nginx.yaml")
    assert (first["staged_questions"], first["weight_total"]) == (1, 8)
    exam = load_exam(checkout / "exams" / "ckad-mock-7.yaml")
    assert exam.draft and exam.planned_questions == 2
    assert exam.title == "CKAD Mock Exam 7" and exam.pass_percent == 75

    # Weights must sum to 100 at full size; the failed write leaves staging as it was.
    assert "weights sum to 16" in _error(
        tools.stage_drill,
        drill_yaml=_drill(id="pod-two", name="api"),
        question_index=2,
        namespace="ckad02-pod",
        weight=8,
    )
    assert sorted(tools._staged) == [1]
    exam = load_exam(checkout / "exams" / "ckad-mock-7.yaml")
    assert exam.draft and [q.drill for q in exam.questions] == ["pod-runs-nginx"]
    assert sorted(p.name for p in (checkout / "drills/ckad-mock-7").glob("*.yaml")) == [
        "q01-pod-runs-nginx.yaml"
    ]

    # A fix round at the same number replaces the file in place; an empty
    # namespace means cluster-scoped.
    again = _call(
        tools.stage_drill, drill_yaml=_drill(), question_index=1, namespace="", weight=4
    )
    assert again["namespace"] is None and again["weight_total"] == 4
    exam = load_exam(checkout / "exams" / "ckad-mock-7.yaml")
    assert exam.questions[0].namespace is None


def test_stage_refuses_ids_owned_by_other_exams_and_bad_weights(checkout: Path) -> None:
    assert "already exists in the catalogue" in _error(
        tools.stage_drill,
        drill_yaml=_drill(id="existing-pod"),
        question_index=1,
        namespace="ckad01-pod",
        weight=4,
    )
    assert "weight must be one of" in _error(
        tools.stage_drill,
        drill_yaml=_drill(),
        question_index=1,
        namespace="ns1",
        weight=3,
    )
    assert "not in the allowed set" in _error(
        tools.stage_drill,
        drill_yaml=_drill().replace("nginx:alpine", "vendor/evil:1"),
        question_index=1,
        namespace="ns1",
        weight=4,
    )
    _call(
        tools.stage_drill,
        drill_yaml=_drill(),
        question_index=1,
        namespace="ns1",
        weight=4,
    )
    assert "already staged as question 1" in _error(
        tools.stage_drill,
        drill_yaml=_drill(),
        question_index=2,
        namespace="ns1",
        weight=4,
    )


def test_diff_task_sees_the_text_at_first_staging(checkout: Path) -> None:
    _call(
        tools.stage_drill,
        drill_yaml=_drill(),
        question_index=1,
        namespace="ns1",
        weight=4,
    )
    assert _call(tools.diff_task, question_index=1)["changed"] is False

    fixed = _call(
        tools.stage_drill,
        drill_yaml=_drill(task="Create a Pod `web` from `nginx:alpine`, quickly."),
        question_index=1,
        namespace="ns1",
        weight=4,
    )
    assert fixed["task_changed"] is True and fixed["replaced"] is False
    diff = _call(tools.diff_task, question_index=1)
    assert diff["changed"] is True
    assert diff["task_at_staging"].startswith("Create a Pod `web` running")

    # A different drill in the slot is a new question with a new baseline.
    replaced = _call(
        tools.stage_drill,
        drill_yaml=_drill(id="other-pod", name="db"),
        question_index=1,
        namespace="ns1",
        weight=4,
    )
    assert replaced["replaced"] is True
    assert _call(tools.diff_task, question_index=1)["changed"] is False
    assert "not staged" in _error(tools.diff_task, question_index=9)


def test_catalogue_tools_see_existing_and_staged_drills(checkout: Path) -> None:
    hits = _call(tools.catalogue_search, query="pod nginx")["hits"]
    assert [h["id"] for h in hits] == ["existing-pod"]
    assert _call(tools.read_drill, drill_id="existing-pod")["yaml"].startswith(
        "id: existing-pod"
    )
    assert "no drill with id" in _error(tools.read_drill, drill_id="nope")

    ok = _call(tools.validate_drill_yaml, drill_yaml=_drill())
    assert ok["ok"] is True and ok["images"] == ["nginx:alpine"]
    bad = _call(tools.validate_drill_yaml, drill_yaml="id: [\n")
    assert bad["ok"] is False and "not valid YAML" in bad["error"]

    near = _call(tools.similar_drills, drill_yaml=_drill())
    assert near["matches"][0]["id"] == "existing-pod"
    assert near["matches"][0]["duplicate"] is True
    assert near["threshold"] == 0.62


def test_proving_tools_refuse_an_unstaged_question(checkout: Path) -> None:
    assert "not staged" in _error(tools.prove_question, question_index=3)
    assert "not staged" in _error(tools.grader_details, question_index=3)
    assert "not staged" in _error(tools.negative_control, question_index=3)
    _call(
        tools.stage_drill,
        drill_yaml=_drill(),
        question_index=1,
        namespace="ns1",
        weight=4,
    )
    assert "has not been proved" in _error(tools.grader_details, question_index=1)
    assert "shell operators" in _error(
        tools.shell, question_index=1, command="kubectl get pods | grep web"
    )
    assert "start with kubectl" in _error(
        tools.shell, question_index=1, command="ls /root"
    )
