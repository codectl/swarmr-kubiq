"""tools: the exam under construction, staged into a kubiq-like tree.

No server, no docs, no model: the kubiq client is a fake that answers the
loader check and the catalogue listing, and the proof itself is a faked
`harness.prove_exam`. Everything else touches the checkout alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from swarmr_kubiq import catalogue, harness, kubiq_api, proofs, tools
from swarmr_kubiq.catalogue import CURRICULUM
from swarmr_kubiq.drill import load_exam
from swarmr_kubiq.kubiq_api import FatalError
from swarmr_kubiq.target import activate, current

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

CONFIGMAP = """\
id: {id}
order: 702
exam: CKAD
title: {title}
topic: Configuration
task: |
  {task}
solution: |
  kubectl create configmap {name} --from-literal=mode=fast
checks:
  - name: {name} holds mode
    resource: {{ kind: ConfigMap, name: {name} }}
    assert:
      - {{ path: "data.mode", equals: fast }}
"""

# The fewest slots per CKAD domain whose share splits into 1, 2, 4 and 8.
_DOMAINS = {
    "Application Design and Build": 3,
    "Application Deployment": 3,
    "Application Observability and Maintenance": 4,
    "Application Environment, Configuration and Security": 4,
    "Services and Networking": 3,
}
QUESTIONS = sum(_DOMAINS.values())


def _drill(
    id: str = "pod-runs-nginx",
    title: str = "Run a Pod",
    task: str = "Create a Pod `web` running `nginx:alpine`.",
    name: str = "web",
) -> str:
    return DRILL.format(id=id, title=title, task=task, name=name)


def _configmap(
    id: str = "cm-mode",
    title: str = "Make a ConfigMap",
    task: str = "Create a ConfigMap `cfg` with key `mode` set to `fast`.",
    name: str = "cfg",
) -> str:
    return CONFIGMAP.format(id=id, title=title, task=task, name=name)


SECRET = """\
id: {id}
order: 703
exam: CKAD
title: {title}
topic: Configuration
task: |
  {task}
solution: |
  kubectl create secret generic {name} --from-literal=token=s3cr3t
checks:
  - name: {name} is opaque
    resource: {{ kind: Secret, name: {name} }}
    assert:
      - {{ path: "type", equals: Opaque }}
"""


def _secret(
    id: str = "secret-token",
    title: str = "Make a Secret",
    task: str = "Store the token `s3cr3t` in a Secret `{name}`.",
    name: str = "tok",
) -> str:
    return SECRET.format(id=id, title=title, task=task.format(name=name), name=name)


def _slots() -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for domain, count in _DOMAINS.items():
        for _ in range(count):
            n = len(slots) + 1
            slots.append(
                {
                    "question": n,
                    "domain": domain,
                    "competency": CURRICULUM["CKAD"][domain][1][0],
                    "namespace": f"ckad{n:02d}-q",
                }
            )
    return slots


class FakeServer:
    """The endpoints tools call outside a proof: loader check, reload, listing."""

    instances: ClassVar[list[FakeServer]] = []
    loader_problem: ClassVar[str] = ""

    def __init__(self, base_url: str, timeout: float = 30) -> None:
        self.base_url = base_url
        self.calls: list[str] = []
        FakeServer.instances.append(self)

    def check(self) -> str:
        self.calls.append("check")
        return FakeServer.loader_problem

    def reload(self) -> dict[str, Any]:
        self.calls.append("reload")
        return {"drills": len(tools._staged), "exams": 1}

    def exams(self) -> list[dict[str, Any]]:
        self.calls.append("exams")
        return [{"id": current().exam, "questions": len(tools._staged)}]

    def sessions(self) -> list[dict[str, Any]]:
        self.calls.append("sessions")
        return []


def _server_calls() -> list[str]:
    return [c for s in FakeServer.instances for c in s.calls]


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # No cluster in tests: the server dry-run accepts everything unless a test
    # swaps in its own kubectl; the kubiq server is a fake; proofs are cached
    # under a temporary home.
    monkeypatch.setattr(harness, "_run", lambda argv, **kw: (0, "", ""))
    monkeypatch.setattr(catalogue, "cluster_version", lambda: "")
    monkeypatch.setattr(kubiq_api, "KubiqClient", FakeServer)
    monkeypatch.setattr(FakeServer, "instances", [])
    monkeypatch.setattr(FakeServer, "loader_problem", "")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "drills" / "ckad-mock-1").mkdir(parents=True)
    (tmp_path / "exams").mkdir()
    (tmp_path / "drills" / "ckad-mock-1" / "q01-existing.yaml").write_text(
        _secret(id="existing-secret", title="Existing", name="old")
    )
    (tmp_path / "exams" / "ckad-mock-1.yaml").write_text(
        "id: ckad-mock-1\ntitle: CKAD Mock Exam 1\nquestions:\n"
        "  - { drill: existing-secret, namespace: ckad01-pod, weight: 100 }\n"
    )
    activate({"kubiq": str(tmp_path), "exam": "ckad-mock-7", "questions": str(QUESTIONS)})
    tools.reset_staging()
    return tmp_path


@pytest.fixture
def planned(checkout: Path) -> Path:
    _call(tools.plan_exam, slots=_slots())
    return checkout


def _call(tool: Any, **kwargs: Any) -> dict[str, Any]:
    text = tool.invoke(kwargs)
    assert not text.startswith("tool error"), text
    return json.loads(text)


def _error(tool: Any, **kwargs: Any) -> str:
    text = tool.invoke(kwargs)
    assert text.startswith("tool error"), text
    return text


def _verdict(
    index: int, drill: str, namespace: str, proven: bool = True
) -> harness.Verdict:
    return harness.Verdict(
        drill=drill,
        question=index,
        namespace=namespace,
        passed=proven,
        negative_control_ok=True,
        results=[{"name": "c", "ok": proven, "detail": "" if proven else "missing"}],
        results_before=[{"name": "c", "ok": False, "detail": "not found"}],
        exit_code=0,
        stdout="",
        stderr="",
        leftovers=[],
        session="abc12345",
        settle_seconds=0.0,
        solve_seconds=1.0,
        setup_settle_seconds=0.0,
    )


def _fake_proofs(monkeypatch: pytest.MonkeyPatch, outcome: dict[str, bool] | None = None):
    """A `prove_exam` that records the indexes asked and grades by drill id."""
    asked: list[list[int] | None] = []

    def prove_exam(client: Any, target: Any, exam: Any, drills: Any, indexes=None):
        asked.append(indexes)
        picked = range(len(exam.questions)) if indexes is None else indexes
        return [
            _verdict(
                i,
                exam.questions[i].drill,
                exam.questions[i].namespace or "default",
                (outcome or {}).get(exam.questions[i].drill, True),
            )
            for i in picked
        ]

    monkeypatch.setattr(harness, "prove_exam", prove_exam)
    return asked


# -- planning --------------------------------------------------------------------


def test_allocate_weights_prefers_few_small_questions() -> None:
    """Fewest 1s, then fewest 2s, then most 8s: 20 over 4 is 8+4+4+4, not 8+8+2+2."""
    assert tools.allocate_weights(20, 4) == [8, 4, 4, 4]
    assert tools.allocate_weights(20, 3) == [8, 8, 4]
    assert tools.allocate_weights(25, 5) == [8, 8, 4, 4, 1]
    assert tools.allocate_weights(15, 4) == [8, 4, 2, 1]
    with pytest.raises(ValueError, match=r"15 points cannot be split into 3.*4-15"):
        tools.allocate_weights(15, 3)
    with pytest.raises(ValueError, match="feasible slot counts: 2-3"):
        tools.allocate_weights(3, 5)


def test_plan_allocates_every_weight_and_the_exam_sums_to_100(checkout: Path) -> None:
    plan = _call(tools.plan_exam, slots=_slots())
    assert plan["kind"] == "plan" and plan["weight_total"] == 100
    assert len(plan["slots"]) == QUESTIONS and plan["staged"] == []
    assert all(s["weight"] in (1, 2, 4, 8) for s in plan["slots"])
    assert [s["weight"] for s in plan["slots"][:3]] == [8, 8, 4]
    assert all(d["points"] == d["share"] for d in plan["domains"].values())
    assert plan["domains"]["Application Deployment"] == {
        "share": 20,
        "slots": 3,
        "points": 20,
    }
    assert plan["slots"][0]["namespace"] == "ckad01-q"
    assert tools._plan[1].weight == 8 and tools._plan[1].competency


def test_plan_refuses_wrong_wording_numbers_and_namespaces(checkout: Path) -> None:
    slots = _slots()
    slots[0]["competency"] = "Build container images"
    err = _error(tools.plan_exam, slots=slots)
    assert "competency 'Build container images' is not verbatim one of" in err
    assert "Define, build and modify container images" in err

    slots = _slots()
    slots[1]["namespace"] = "ckad01-two"
    assert "carries number 01, must be 02" in _error(tools.plan_exam, slots=slots)
    slots[1]["namespace"] = "ckad02-Two"
    assert "must match ckad02-<word>" in _error(tools.plan_exam, slots=slots)

    slots = _slots()
    slots[0]["namespace"] = "ckad01-pod"  # ckad-mock-1's
    assert "belongs to another exam" in _error(tools.plan_exam, slots=slots)

    slots = _slots()
    slots[2]["question"] = 2
    assert "question 2 is planned twice" in _error(tools.plan_exam, slots=slots)
    assert f"has {QUESTIONS} questions; 16 slots" in _error(
        tools.plan_exam, slots=_slots()[:-1]
    )

    slots = _slots()
    slots[0]["domain"] = "Networking"
    assert "domain 'Networking' is not one of" in _error(tools.plan_exam, slots=slots)
    for s in slots:
        s["domain"] = "Application Deployment"
        s["competency"] = "Kustomize"
    assert "'Application Design and Build' has a 20 % share and no slot" in _error(
        tools.plan_exam, slots=slots
    )
    assert tools._plan == {}, "a refused plan changes nothing"


# -- staging ---------------------------------------------------------------------


def test_stage_requires_a_plan(checkout: Path) -> None:
    assert "no plan for question 1; the commander must call plan_exam first" in _error(
        tools.stage_drill, drill_yaml=_drill(), question_index=1
    )


def test_stage_writes_a_draft_with_the_planned_weight(planned: Path) -> None:
    first = _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    assert first["path"] == str(planned / "drills/ckad-mock-7/q01-pod-runs-nginx.yaml")
    assert (first["namespace"], first["weight"]) == ("ckad01-q", 8)
    assert (first["staged_questions"], first["weight_total"]) == (1, 8)
    assert (first["replaced"], first["proof_stale"]) == (False, False)
    assert set(first) >= {"kind", "exam_file", "planned_questions"}
    exam = load_exam(planned / "exams" / "ckad-mock-7.yaml")
    assert exam.draft and exam.planned_questions == QUESTIONS
    assert exam.title == "CKAD Mock Exam 7" and exam.pass_percent == 75
    assert exam.questions[0].namespace == "ckad01-q" and exam.questions[0].weight == 8
    assert _server_calls() == ["check"], "the loader was asked once, nothing reloaded"


def test_stage_refuses_foreign_ids_siblings_and_a_changed_task(planned: Path) -> None:
    assert "already exists in the catalogue" in _error(
        tools.stage_drill, drill_yaml=_secret(id="existing-secret"), question_index=1
    )
    assert "not in the allowed set" in _error(
        tools.stage_drill,
        drill_yaml=_drill().replace("nginx:alpine", "vendor/evil:1"),
        question_index=1,
    )
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    assert "already staged as question 1" in _error(
        tools.stage_drill, drill_yaml=_drill(), question_index=2
    )
    # A renamed copy of a sibling is the same question.
    err = _error(
        tools.stage_drill,
        drill_yaml=_drill(id="pod-two", name="api"),
        question_index=2,
    )
    assert "same question as staged question 1 ('pod-runs-nginx')" in err
    assert "1.00 ≥ 0.62" in err
    # The task is frozen at first staging; the solution may change.
    assert "task text of 'pod-runs-nginx' is frozen" in _error(
        tools.stage_drill,
        drill_yaml=_drill(task="Create a Pod `web` from `nginx:alpine`, quickly."),
        question_index=1,
    )
    fixed = _call(
        tools.stage_drill,
        drill_yaml=_drill().replace("image: nginx:alpine", "image: nginx:1.27-alpine"),
        question_index=1,
    )
    assert fixed["replaced"] is False and fixed["proof_stale"] is True
    # A different drill in the slot is a new question with a new baseline.
    replaced = _call(
        tools.stage_drill, drill_yaml=_configmap(id="other-cm"), question_index=1
    )
    assert replaced["replaced"] is True
    assert tools._task_at_staging[1][0] == "other-cm"
    assert sorted(p.name for p in (planned / "drills/ckad-mock-7").glob("*.yaml")) == [
        "q01-other-cm.yaml"
    ]


def test_stage_refuses_a_near_copy_of_the_catalogue(planned: Path) -> None:
    err = _error(
        tools.stage_drill, drill_yaml=_secret(id="secret-copy"), question_index=1
    )
    assert "same question as catalogue drill 'existing-secret'" in err
    assert not (planned / "exams" / "ckad-mock-7.yaml").exists()
    # Its own drills from an earlier run are not "the catalogue": a slot may be
    # replaced by a near-copy of what it held.
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    tools.reset_staging()
    tools.resume_staging(current())
    again = _call(
        tools.stage_drill, drill_yaml=_drill(id="pod-again", name="api"), question_index=1
    )
    assert again["replaced"] is True


_EXEC_CHECK = """\
  - name: {name} answers
    exec: {{ shell: true, command: [bash, -lc, "{command} && echo ok"] }}
    assert:
      - {{ exit: 0, contains: ok }}
"""


def test_stage_refuses_a_shell_exec_check_without_the_namespace(planned: Path) -> None:
    """kubiq runs `shell: true` checks in whatever namespace the candidate's
    context holds; a kubectl without `-n <ns>` grades the wrong place."""
    unscoped = _drill(name="api") + _EXEC_CHECK.format(
        name="api", command="kubectl get pod api"
    )
    err = _error(tools.stage_drill, drill_yaml=unscoped, question_index=1)
    assert "without `-n ckad01-" in err and "checks[1]" in err
    # The namespace named anywhere in the command, or a cluster-scoped kubectl,
    # is fine.
    ns = json.loads(tools.plan_exam.invoke({"slots": _slots()}))["slots"][0]["namespace"]
    scoped = _drill(name="api") + _EXEC_CHECK.format(
        name="api", command=f"kubectl get pod api -n {ns}"
    )
    _call(tools.stage_drill, drill_yaml=scoped, question_index=1)
    nodes = _configmap(id="node-list", name="cli") + _EXEC_CHECK.format(
        name="cli", command="kubectl get nodes"
    )
    _call(tools.stage_drill, drill_yaml=nodes, question_index=2)


def test_stage_rolls_back_when_the_loader_refuses(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Python validator is a mirror; kubiq's own loader is the authority."""
    drill_file = planned / "drills/ckad-mock-7/q01-pod-runs-nginx.yaml"
    monkeypatch.setattr(FakeServer, "loader_problem", "Error: parsing q01: some rule")
    err = _error(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    assert "kubiq itself refuses" in err and "some rule" in err
    assert not (planned / "exams" / "ckad-mock-7.yaml").exists()
    assert tools._staged == {}

    monkeypatch.setattr(FakeServer, "loader_problem", "")
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    before = drill_file.read_text()
    monkeypatch.setattr(FakeServer, "loader_problem", "Error: parsing q01: some rule")
    assert "some rule" in _error(
        tools.stage_drill,
        drill_yaml=_drill().replace("image: nginx:alpine", "image: nginx:1.27-alpine"),
        question_index=1,
    )
    assert drill_file.read_text() == before, "the accepted drill is back on disk"
    assert "nginx:alpine" in tools._staged[1].solution


def test_stage_refuses_a_setup_the_api_server_rejects(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run catches a wrong enum before any file or session."""
    calls: list[list[str]] = []

    def kubectl(argv: list[str], **kw: Any) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ["kubectl", "apply"]:
            assert "--dry-run=server" in argv and "RestartNotRequired" in kw["stdin"]
            return 1, "", 'resizePolicy: Unsupported value: "RestartNotRequired"'
        return 0, "", ""

    monkeypatch.setattr(harness, "_run", kubectl)
    bad = _drill().replace(
        "solution:",
        "setup: |\n  apiVersion: v1\n  kind: Pod\n  metadata: {name: p}\n"
        "  spec:\n    containers:\n    - {name: c, image: nginx,"
        " resizePolicy: RestartNotRequired}\n"
        "solution:",
    )
    err = _error(tools.stage_drill, drill_yaml=bad, question_index=1)
    assert "Unsupported value" in err and "docs page" in err
    assert not (planned / "drills" / "ckad-mock-7").exists()
    assert ["kubectl", "create", "namespace", harness.DRY_RUN_NAMESPACE] in calls
    assert not any("delete" in c for c in calls), "the scratch namespace is never deleted"


def test_catalogue_tools_see_existing_and_staged_drills(planned: Path) -> None:
    hits = _call(tools.catalogue_search, query="secret token")["hits"]
    assert [h["id"] for h in hits] == ["existing-secret"]
    assert _call(tools.read_drill, drill_id="existing-secret")["yaml"].startswith(
        "id: existing-secret"
    )
    assert "no drill with id" in _error(tools.read_drill, drill_id="nope")

    # stage_drill validates, scores and writes in one call; the nearest
    # neighbours ride on its result so the author never pre-flights.
    assert "not valid YAML" in _error(
        tools.stage_drill, drill_yaml="id: [\n", question_index=1
    )
    staged = _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    assert [m["id"] for m in staged["nearest"]] == ["existing-secret"]
    again = _call(tools.stage_drill, drill_yaml=_configmap(id="cm-two"), question_index=2)
    assert {m["id"] for m in again["nearest"]} == {
        "existing-secret",
        "staged q1 pod-runs-nginx",
    }


# -- proving ---------------------------------------------------------------------


def test_proving_tools_refuse_an_unstaged_question(planned: Path) -> None:
    assert "not staged" in _error(tools.prove_questions, question_indexes=[3])
    assert "not staged" in _error(tools.grader_details, question_index=3)
    assert "nothing is staged" in _error(tools.prove_all)
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    assert "has not been proved" in _error(tools.grader_details, question_index=1)


def test_prove_questions_enforces_the_per_drill_budget(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three proofs of one drill id at a question; the fourth is refused, a
    replacement under a new id starts afresh."""
    asked = _fake_proofs(monkeypatch, {"cm-mode": False})
    _call(tools.stage_drill, drill_yaml=_configmap(), question_index=2)
    for _ in range(tools.PROOF_BUDGET):
        assert _call(tools.prove_questions, question_indexes=[2])["failed"] == [2]
    err = _error(tools.prove_questions, question_indexes=[2])
    assert "proof budget exhausted for question 2 ('cm-mode')" in err
    assert len(asked) == tools.PROOF_BUDGET, "the refused call never reached the cluster"
    _call(tools.stage_drill, drill_yaml=_configmap(id="cm-replacement"), question_index=2)
    _call(tools.prove_questions, question_indexes=[2])
    assert len(asked) == tools.PROOF_BUDGET + 1


def test_prove_all_skips_proven_questions_and_saves_proofs(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _fake_proofs(monkeypatch, {"cm-mode": False})
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    _call(tools.stage_drill, drill_yaml=_configmap(), question_index=2)

    batch = _call(tools.prove_all)
    assert batch["kind"] == "proof_batch"
    assert (batch["proven"], batch["failed"], batch["skipped"]) == ([1], [2], [])
    assert asked == [[0, 1]]
    assert _server_calls().count("reload") == 1
    rows = batch["questions"]
    assert set(rows[0]) == {"question", "drill", "proven"}
    assert rows[1]["why"] == "checks failed" and rows[1]["failing"][0]["check"] == "c"
    full = _call(tools.grader_details, question_index=1)
    assert full["kind"] == "verdict" and full["results_before"][0]["ok"] is False
    assert full["server_reloaded"] is True
    saved = proofs.load_proofs(current())
    assert len(saved) == 2 and proofs.cache_path(current()).is_file()
    assert str(proofs.cache_path(current())).startswith(str(planned / "cache"))

    # Only the failed question is proved again; nothing changed, so no reload.
    batch = _call(tools.prove_all)
    assert (batch["proven"], batch["failed"], batch["skipped"]) == ([], [2], [1])
    assert asked == [[0, 1], [1]] and _server_calls().count("reload") == 1

    # A fix is a new file: reload once, prove the fix alone, the proof persists.
    fixed = _call(
        tools.stage_drill,
        drill_yaml=_configmap().replace("mode=fast", "mode=fast --dry-run=none"),
        question_index=2,
    )
    assert fixed["proof_stale"] is True
    asked = _fake_proofs(monkeypatch)
    one = _call(tools.prove_questions, question_indexes=[2])
    assert (one["proven"], one["skipped"]) == ([2], []) and asked == [[1]]
    assert _server_calls().count("reload") == 2
    assert _call(tools.prove_all)["skipped"] == [1, 2]
    assert len(proofs.load_proofs(current())) == 3

    # prove_questions proves whatever it is given, proven or not, in one session.
    both = _call(tools.prove_questions, question_indexes=[2, 1])
    assert both["proven"] == [1, 2] and asked == [[1], [0, 1]]


def test_a_fatal_error_is_sticky(planned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the cluster says 'do not retry', no tool touches the server again."""
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)

    def prove_exam(*args: Any, **kwargs: Any) -> list[harness.Verdict]:
        raise FatalError("session x on exam 'other' is open. Do not retry.")

    monkeypatch.setattr(harness, "prove_exam", prove_exam)
    first = _error(tools.prove_questions, question_indexes=[1])
    assert first == "tool error: FATAL: session x on exam 'other' is open. Do not retry."
    calls = len(_server_calls())
    assert _error(tools.prove_all) == first
    assert _error(tools.prove_questions, question_indexes=[1]) == first
    assert "FATAL" in _error(tools.stage_drill, drill_yaml=_configmap(), question_index=2)
    assert len(_server_calls()) == calls, "no call reached the server after the FATAL"
    assert tools._staged.keys() == {1}, "the refused staging rolled back"
    tools.reset_staging()
    assert tools._fatal is None


def test_a_stale_catalogue_is_a_kubiq_error(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proofs(monkeypatch)
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    monkeypatch.setattr(FakeServer, "exams", lambda self: [])
    assert "does not list exam 'ckad-mock-7'" in _error(tools.prove_all)
    monkeypatch.setattr(
        FakeServer, "exams", lambda self: [{"id": "ckad-mock-7", "questions": 3}]
    )
    assert "with 3 questions, 1 are staged" in _error(tools.prove_all)


# -- resuming --------------------------------------------------------------------


def test_a_rerun_resumes_what_an_earlier_run_staged(planned: Path) -> None:
    """A run that died mid-way left files; the next `build` continues from them."""
    from swarmr_kubiq.catalogue import profile_catalogue, render_facts

    _call(
        tools.stage_drill,
        drill_yaml=_drill(
            id="resumed-pod", title="Resumed", task="Create `a`.", name="a"
        ).replace("order: 701", "order: 702"),
        question_index=2,
    )
    tools.reset_staging()  # process restart
    assert tools._plan == {}

    resumed = tools.resume_staging(current())
    assert [(r["question"], r["drill"], r["weight"], r["proven"]) for r in resumed] == [
        (2, "resumed-pod", 8, False)
    ]
    assert resumed[0]["problem"] == ""
    assert tools._plan[2].namespace == "ckad02-q" and tools._plan[2].domain == ""
    assert tools._task_at_staging[2] == ("resumed-pod", "Create `a`.\n")
    assert "frozen since first staging" in _error(
        tools.stage_drill,
        drill_yaml=_drill(
            id="resumed-pod", title="Resumed", task="Create `b`.", name="a"
        ),
        question_index=2,
    )

    facts = render_facts(profile_catalogue(), current(), resumed=resumed)
    assert "RESUMED from an earlier run" in facts
    assert "2. resumed-pod · Pods · Resumed · weight 8 · ns ckad02-q" in facts
    assert "- existing-secret · Configuration · Existing" in facts
    assert "- resumed-pod · Pods" not in facts, "own questions are staged, not 'existing'"

    # Planning again keeps the staged question and rewrites the exam around it.
    plan = _call(tools.plan_exam, slots=_slots())
    assert plan["staged"] == [2] and tools._plan[2].domain
    assert (
        load_exam(planned / "exams/ckad-mock-7.yaml").questions[0].drill == "resumed-pod"
    )


def test_resume_is_a_noop_for_a_fresh_exam(checkout: Path) -> None:
    assert tools.resume_staging(current()) == []


def test_resume_restores_a_proof_only_when_the_key_matches(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proofs(monkeypatch)
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    _call(tools.prove_all)

    tools.reset_staging()
    resumed = tools.resume_staging(current())
    assert resumed[0]["proven"] is True
    full = _call(tools.grader_details, question_index=1)
    assert full["resumed"] is True and full["question"] == 1
    assert _call(tools.prove_all)["skipped"] == [1], "nothing to prove again"

    # The file changed under the proof: the proof no longer applies.
    drill_file = planned / "drills/ckad-mock-7/q01-pod-runs-nginx.yaml"
    drill_file.write_text(drill_file.read_text().replace("nginx:alpine", "nginx:1.27"))
    tools.reset_staging()
    resumed = tools.resume_staging(current())
    assert resumed[0]["proven"] is False
    assert "has not been proved" in _error(tools.grader_details, question_index=1)


def test_resume_revalidates_with_todays_rules(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swarmr_kubiq.catalogue import profile_catalogue, render_facts

    with_setup = _drill().replace(
        "solution:",
        "setup: |\n  apiVersion: v1\n  kind: ConfigMap\n  metadata: {name: c}\nsolution:",
    )
    _call(tools.stage_drill, drill_yaml=with_setup, question_index=1)
    # An older version staged this; today the API server rejects its setup.
    monkeypatch.setattr(
        harness,
        "_run",
        lambda argv, **kw: (1, "", "bad enum") if "apply" in argv else (0, "", ""),
    )
    tools.reset_staging()
    resumed = tools.resume_staging(current())
    assert resumed[0]["problem"] == "bad enum"
    facts = render_facts(profile_catalogue(), current(), resumed=resumed)
    assert "FAIL today's validation" in facts and "1. pod-runs-nginx: bad enum" in facts


def test_resume_reports_a_loader_that_cannot_be_asked(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _call(tools.stage_drill, drill_yaml=_drill(), question_index=1)
    _call(tools.stage_drill, drill_yaml=_configmap(), question_index=2)

    def down(self: FakeServer) -> str:
        raise FatalError("kubiq is not running at http://x; start it. Do not retry.")

    monkeypatch.setattr(FakeServer, "check", down)
    tools.reset_staging()
    resumed = tools.resume_staging(current())
    assert [r["problem"] for r in resumed] == [
        "loader check unavailable: kubiq is not running at http://x; start it. "
        "Do not retry."
    ] * 2
    assert tools._fatal is None, "resume reports; it does not end the run"


def test_search_docs_refuses_to_repeat_a_query(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-issued query is the start of a loop; the tool says stop instead."""
    searched: list[str] = []

    class Docs:
        note = ""

        def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
            searched.append(query)
            return [{"title": "Images", "url": "https://kubernetes.io/x", "snippet": ""}]

        def close(self) -> None:
            pass

    monkeypatch.setattr(tools, "_docs", lambda: Docs())
    first = _call(tools.search_docs, query="helm install release chart")
    assert len(first["hits"]) == 1 and "helm.sh is not indexed" in first["note"]
    again = _call(tools.search_docs, query="chart release install helm")
    assert len(again["hits"]) == 1 and "already searched" in again["note"]
    assert searched == ["helm install release chart"], "second call never hit lodestar"
    tools.reset_staging()
    _call(tools.search_docs, query="helm install release chart")
    assert len(searched) == 2, "a new run forgets the previous run's searches"


def test_read_doc_serves_a_repeated_url_from_memory(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages: list[str] = []

    class Docs:
        note = "tool error: lodestar get_page: unknown page"

        def page(self, url: str, **_: Any) -> str:
            pages.append(url)
            return ""

        def close(self) -> None:
            pass

    monkeypatch.setattr(tools, "_docs", lambda: Docs())
    first = _call(tools.read_doc, url="https://kubernetes.io/docs/x/")
    assert first["text"] == "" and "unknown page" in first["note"]
    again = _call(tools.read_doc, url="https://kubernetes.io/docs/x")
    assert "already read" in again["note"] and "not retrievable" in again["note"]
    assert pages == ["https://kubernetes.io/docs/x"], "lodestar asked once"
    # A section is a different read: fetched once per section.
    _call(tools.read_doc, url="https://kubernetes.io/docs/x#tls")
    assert pages == ["https://kubernetes.io/docs/x"] * 2


def test_stage_refuses_a_kind_the_cluster_does_not_serve(
    planned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the cluster is, a kind it does not list in api-resources can
    never be proven on it; the refusal names the kind, nothing is hardcoded."""
    monkeypatch.setattr(
        harness, "served_kinds", lambda: {("v1", "Pod"), ("v1", "ConfigMap")}
    )
    tools._served = None
    policy = _configmap(id="policy-q").replace(
        "solution: |\n",
        "solution: |\n  kubectl apply -f - <<'EOF'\n"
        "  apiVersion: admissionregistration.k8s.io/v1\n"
        "  kind: MutatingAdmissionPolicy\n  metadata:\n    name: p\n  EOF\n",
    )
    err = _error(tools.stage_drill, drill_yaml=policy, question_index=1)
    assert "does not serve admissionregistration.k8s.io/v1 MutatingAdmissionPolicy" in err
    _call(tools.stage_drill, drill_yaml=_configmap(), question_index=1)
