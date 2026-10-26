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
        with pytest.raises(DrillError, match="exactly one of `resource`/`exec`"):
            parse_drill(text)

    def test_grader_kinds_are_mirrored(self) -> None:
        d = parse_drill(MINIMAL)
        d.checks[0].resource = {"kind": "ValidatingAdmissionPolicy", "name": "x"}
        with pytest.raises(DrillError, match="group/version/Kind/plural"):
            validate_drill(d)
        d.checks[0].resource["kind"] = (
            "admissionregistration.k8s.io/v1/ValidatingAdmissionPolicy/"
            "validatingadmissionpolicies/cluster"
        )
        validate_drill(d)

    def test_setup_may_only_hold_kinds_kubiq_applies(self) -> None:
        d = parse_drill(MINIMAL)
        d.setup = "apiVersion: example.com/v1\nkind: AppView\nmetadata:\n  name: x\n"
        with pytest.raises(DrillError, match=r"setup kind 'AppView'.*start hook"):
            validate_drill(d)
        d.setup = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
        validate_drill(d)

    def test_exec_command_parts_must_be_strings(self) -> None:
        """An unquoted `grep 'INFO: serving'` becomes a YAML mapping; kubiq refuses."""
        text = MINIMAL.split("checks:")[0] + (
            "checks:\n  - name: c\n    exec:\n      shell: true\n      command:\n"
            "        - bash\n        - -lc\n"
            "        - kubectl logs x | grep -q 'INFO: serving'\n"
            "    assert:\n      - { exit: 0 }\n"
        )
        with pytest.raises(DrillError, match=r"command\[2\] must be a string"):
            parse_drill(text)

    def test_assertions_need_a_predicate(self) -> None:
        text = MINIMAL.replace("equals: Running", "")
        with pytest.raises(DrillError, match="no predicate"):
            parse_drill(text)

    def test_kubiq_loader_rules_are_mirrored(self) -> None:
        """What kubiq's `Drill::validate` refuses, we refuse before writing."""
        d = parse_drill(MINIMAL)
        d.docs = [{"label": "a", "url": "u"}, {"label": "b", "url": "v"}]
        with pytest.raises(DrillError, match="at most one `docs`"):
            validate_drill(d)
        d = parse_drill(MINIMAL)
        d.checks[0].resource = {"kind": "Pod"}
        with pytest.raises(DrillError, match="needs `name` or `selector`"):
            validate_drill(d)

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


_SETUP_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: relay-web
spec:
  selector: {{ matchLabels: {{ app: relay }} }}
  template:
    metadata: {{ labels: {{ app: relay }} }}
    spec:
      containers: [{{ name: web, image: nginx:alpine }}]
"""


def test_manifest_kinds_and_setup_objects_read_every_source() -> None:
    d = parse_drill(MINIMAL)
    d.setup = _SETUP_DEPLOYMENT.format()
    d.solution = (
        "kubectl apply -f - <<'EOF'\napiVersion: admissionregistration.k8s.io/v1\n"
        "kind: MutatingAdmissionPolicy\nmetadata:\n  name: x\nEOF\n"
    )
    d.checks.append(
        Check(
            name="policy",
            resource={"kind": "policy/v1/PodDisruptionBudget/poddisruptionbudgets"},
            assert_=[{"path": "spec.minAvailable", "equals": 1}],
        )
    )
    assert d.manifest_kinds() == {
        ("apps/v1", "Deployment"),
        ("admissionregistration.k8s.io/v1", "MutatingAdmissionPolicy"),
        ("policy/v1", "PodDisruptionBudget"),
    }
    assert d.setup_objects() == {"relay-web": "Deployment"}


def test_strict_refuses_exec_into_a_controller_name() -> None:
    d = parse_drill(MINIMAL)
    d.setup = _SETUP_DEPLOYMENT.format()
    d.checks.append(
        Check(
            name="env",
            exec={"pod": "relay-web", "command": ["sh", "-c", "echo $X"]},
            assert_=[{"exit": 0}],
        )
    )
    validate_drill(d, "d")  # the loader accepts it; it would just never pass
    with pytest.raises(DrillError, match="is the setup's Deployment, not a Pod"):
        validate_drill(d, "d", strict=True)


def test_strict_refuses_an_escaped_dot_in_a_path() -> None:
    d = parse_drill(MINIMAL)
    d.checks[0].assert_ = [{"path": r"data.mode\.conf", "contains": "live"}]
    with pytest.raises(DrillError, match=r'\["mode\.…"\]'):
        validate_drill(d, "d", strict=True)


def test_strict_refuses_a_jsonpath_filter_in_a_path() -> None:
    d = parse_drill(MINIMAL)
    d.checks[0].assert_ = [
        {"path": '[*].status.conditions[?(@.type=="Ready")].status', "equals": "True"}
    ]
    with pytest.raises(DrillError, match="JSONPath filter"):
        validate_drill(d, "d", strict=True)


@pytest.mark.parametrize(
    "solution",
    [
        "# edit the manifest and apply it\n# kubectl apply -f pod.yaml\n",
        "kubectl get pods\nkubectl describe pod sample\n",
        "vim pod.yaml\n",
    ],
)
def test_strict_refuses_a_solution_that_changes_nothing(solution: str) -> None:
    d = parse_drill(MINIMAL)
    d.solution = solution
    validate_drill(d, "d")
    with pytest.raises(DrillError, match="changes anything"):
        validate_drill(d, "d", strict=True)


@pytest.mark.parametrize(
    "solution",
    [
        "kubectl patch deploy x -p '{}'\n",
        "helm install web ./chart\n",
        "kubectl get pod sample -o yaml > /root/pod.yaml\n",
        "kubectl apply -f - <<'EOF'\napiVersion: v1\nkind: Pod\nEOF\n",
    ],
)
def test_strict_accepts_a_solution_that_acts(solution: str) -> None:
    d = parse_drill(MINIMAL)
    d.solution = solution
    validate_drill(d, "d", strict=True)


_CRD = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: databases.dbstack.example.com
spec:
  group: dbstack.example.com
  names: {{ kind: Database, plural: databases }}
  scope: Namespaced
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema: {{ openAPIV3Schema: {{ type: object }} }}
"""


def test_kinds_declared_by_the_drills_own_crd_are_not_missing() -> None:
    """A CRD question installs its API in setup; the cluster serves it only
    then, so the served-kinds gate must exempt what the drill declares."""
    d = parse_drill(MINIMAL)
    d.setup = _CRD.format()
    d.files = {
        "/opt/student-node/start.d/x.sh": (
            "kubectl apply -f - <<'EOF'\napiVersion: dbstack.example.com/v1alpha1\n"
            "kind: Database\nmetadata:\n  name: primary\nEOF\n"
        )
    }
    assert d.declared_kinds() == {("dbstack.example.com/v1alpha1", "Database")}
    assert d.manifest_kinds() - d.declared_kinds() == {
        ("apiextensions.k8s.io/v1", "CustomResourceDefinition")
    }


@pytest.mark.parametrize(
    "setup",
    [
        "apiVersion: v1\nkind: Secret\nmetadata: {name: tls}\ntype: kubernetes.io/tls\n"
        "stringData:\n  tls.crt: |\n    -----BEGIN CERTIFICATE-----\n    MIIB\n"
        "  tls.key: |\n    -----BEGIN PRIVATE KEY-----\n    MIIEvQ\n",
        "apiVersion: v1\nkind: Secret\nmetadata: {name: tls}\ndata:\n"
        "  tls.key: " + "QUJD" * 20 + "\n",
    ],
)
def test_strict_refuses_a_private_key_in_the_drill(setup: str) -> None:
    d = parse_drill(MINIMAL)
    d.setup = setup
    validate_drill(d, "d")
    with pytest.raises(DrillError, match="embeds a private key"):
        validate_drill(d, "d", strict=True)


def test_strict_allows_minting_the_key_in_a_hook() -> None:
    d = parse_drill(MINIMAL)
    d.files = {
        "/opt/student-node/start.d/x.sh": (
            "openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/k -out /tmp/c "
            "-subj /CN=x -days 1\n"
            "kubectl create secret tls t --key /tmp/k --cert /tmp/c\n"
        )
    }
    validate_drill(d, "d", strict=True)
