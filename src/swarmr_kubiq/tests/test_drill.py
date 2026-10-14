"""The drill/exam schema: parse, validate, dump, and what is refused."""

from __future__ import annotations

import pytest

from swarmr_kubiq.drill import (
    Check,
    Drill,
    DrillError,
    Exam,
    Question,
    dump_drill,
    dump_exam,
    parse_drill,
    parse_exam,
    validate_drill,
)

MINIMAL = """\
id: sample-pod
order: 701
exam: CKAD
title: "Pods: a sample"
topic: Pods
task: |
  Create a Pod named `sample` running `nginx:alpine`.
solution: |
  kubectl run sample --image=nginx:alpine
checks:
  - name: Pod sample is running
    resource: { kind: Pod, name: sample }
    assert:
      - { path: status.phase, equals: Running }
"""


class TestParseDrill:
    def test_minimal_round_trips_through_dump(self) -> None:
        d = parse_drill(MINIMAL)
        again = parse_drill(dump_drill(d))
        assert again.id == "sample-pod"
        assert again.title == "Pods: a sample"
        assert again.checks[0].resource == {"kind": "Pod", "name": "sample"}
        assert again.solution.strip() == "kubectl run sample --image=nginx:alpine"

    def test_title_with_colon_is_quoted_on_dump(self) -> None:
        d = parse_drill(MINIMAL)
        assert 'title: "Pods: a sample"' in dump_drill(d)

    def test_multiline_fields_use_block_scalars(self) -> None:
        d = parse_drill(MINIMAL)
        d.setup = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
        assert "setup: |" in dump_drill(d)

    @pytest.mark.parametrize(
        ("mutation", "needle"),
        [
            ("id: Sample-Pod", "lowercase-dash"),
            ("order: seven", "`order` must be an integer"),
            ("checks: []", "`checks` must be a non-empty list"),
        ],
    )
    def test_refuses_what_kubiq_refuses(self, mutation: str, needle: str) -> None:
        text = "\n".join(
            mutation if line.split(":")[0] == mutation.split(":")[0] else line
            for line in MINIMAL.splitlines()
        )
        if mutation.startswith("checks"):
            text = MINIMAL.split("checks:")[0] + "checks: []\n"
        with pytest.raises(DrillError, match=needle):
            parse_drill(text)

    def test_check_needs_resource_or_exec(self) -> None:
        text = MINIMAL.replace("    resource: { kind: Pod, name: sample }\n", "")
        with pytest.raises(DrillError, match="needs `resource` or `exec`"):
            parse_drill(text)

    def test_unknown_assert_key_is_named(self) -> None:
        text = MINIMAL.replace("equals: Running", "equal: Running")
        with pytest.raises(DrillError, match="unknown keys \\['equal'\\]"):
            parse_drill(text)


class TestStrictRules:
    def test_vendor_reference_is_refused_in_strict_mode(self) -> None:
        d = parse_drill(MINIMAL.replace("running `nginx:alpine`", "from kodekloud"))
        with pytest.raises(DrillError, match="kodekloud"):
            validate_drill(d, strict=True)

    def test_unknown_image_is_refused_only_in_strict_mode(self) -> None:
        d = parse_drill(MINIMAL)
        d.setup = (
            "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - image: vendor/app:1\n"
        )
        validate_drill(d)
        with pytest.raises(DrillError, match="not in the allowed set"):
            validate_drill(d, strict=True)

    def test_helm_template_values_are_not_images(self) -> None:
        d = parse_drill(MINIMAL)
        d.files = {"/opt/c/templates/d.yaml": "image: {{ .Values.image.repository }}\n"}
        assert d.images() == ["nginx:alpine"]  # only the solution's --image flag
        validate_drill(d, strict=True)

    def test_imperative_image_flag_is_scanned(self) -> None:
        d = parse_drill(MINIMAL)
        d.solution = "kubectl run x --image=vendor/app:1\nkubectl run y --image busybox\n"
        assert d.images() == ["busybox", "vendor/app:1"]
        with pytest.raises(DrillError, match="vendor/app:1"):
            validate_drill(d, strict=True)


class TestExam:
    def _exam(self, weights: list[int], draft: bool = False) -> Exam:
        return Exam(
            id="ckad-mock-7",
            title="CKAD Mock Exam 7",
            certification="CKAD",
            description="d",
            duration_minutes=120,
            pass_percent=75,
            questions=[
                Question(f"d{i}", w, f"ckad{i:02d}-x") for i, w in enumerate(weights)
            ],
            draft=draft,
        )

    def test_weights_must_sum_to_100_unless_draft(self) -> None:
        text = dump_exam(self._exam([50, 50]))
        assert parse_exam(text).questions[1].weight == 50
        with pytest.raises(DrillError, match="sum to 90"):
            parse_exam(dump_exam(self._exam([50, 40])))
        parse_exam(dump_exam(self._exam([50, 40], draft=True)))

    def test_duplicate_drill_is_refused(self) -> None:
        e = self._exam([50, 50])
        e.questions[1].drill = e.questions[0].drill
        with pytest.raises(DrillError, match="listed twice"):
            parse_exam(dump_exam(e))

    def test_check_to_yaml_orders_assert_last(self) -> None:
        c = Check(name="n", assert_=[{"exists": True}], resource={"kind": "Pod"})
        assert list(c.to_yaml()) == ["name", "resource", "assert"]


class TestAgainstRealCatalogue:
    def test_every_shipped_drill_parses(self) -> None:
        """The schema must accept everything kubiq already ships."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[4] / "kubiq" / "drills"
        if not root.is_dir():
            pytest.skip("kubiq checkout not beside this repo")
        paths = sorted(root.rglob("*.yaml"))
        assert paths
        for p in paths:
            parse_drill(p.read_text(), str(p))


def test_drill_dataclass_images_dedup() -> None:
    d = Drill(
        id="x",
        order=1,
        exam="CKAD",
        title="t",
        topic="Pods",
        task="t",
        solution="image: nginx:1.17\nimage: nginx:1.17\n",
        checks=[Check("n", [{"exists": True}], resource={"kind": "Pod"})],
    )
    assert d.images() == ["nginx:1.17"]
