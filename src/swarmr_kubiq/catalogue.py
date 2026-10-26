"""What already exists in the kubiq checkout: every drill, every exam.

One responsibility: profiling the target before the first token, and rendering
that profile as the `facts` block every prompt receives. The author must not
repeat a question that exists, the critic must be able to compare against the
whole catalogue, and the commander must know which topics the curriculum still
lacks — all of that comes from here, read once at startup.

Mirrors `discovery.py` in the sibling teams. Every call is a read.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from swarmr_kubiq.drill import Drill, DrillError, Exam, load_drill, load_exam
from swarmr_kubiq.kubiq_api import run
from swarmr_kubiq.target import Target, TargetError, current, minor

__all__ = [
    "CURRICULUM",
    "Catalogue",
    "banner",
    "check_version",
    "cluster_version",
    "profile_catalogue",
    "render_facts",
]

# The real exams' curricula, verbatim from github.com/cncf/curriculum
CURRICULUM: dict[str, dict[str, tuple[int, tuple[str, ...]]]] = {
    "CKAD": {
        "Application Design and Build": (
            20,
            (
                "Define, build and modify container images",
                "Choose and use the right workload resource (Deployment, DaemonSet, "
                "CronJob, etc.)",
                "Understand multi-container Pod design patterns (e.g. sidecar, init "
                "and others)",
                "Utilize persistent and ephemeral volumes",
            ),
        ),
        "Application Deployment": (
            20,
            (
                "Use Kubernetes primitives to implement common deployment strategies "
                "(e.g. blue/green or canary)",
                "Understand Deployments and how to perform rolling updates",
                "Use the Helm package manager to deploy existing packages",
                "Kustomize",
            ),
        ),
        "Application Observability and Maintenance": (
            15,
            (
                "Understand API deprecations",
                "Implement probes and health checks",
                "Use built-in CLI tools to monitor Kubernetes applications",
                "Utilize container logs",
                "Debugging in Kubernetes",
            ),
        ),
        "Application Environment, Configuration and Security": (
            25,
            (
                "Discover and use resources that extend Kubernetes (CRD, Operators)",
                "Understand authentication, authorization and admission control",
                "Understand requests, limits, quotas",
                "Define resource requirements",
                "Understand ConfigMaps",
                "Create & consume Secrets",
                "Understand ServiceAccounts",
                "Understand Application Security (SecurityContexts, Capabilities, etc.)",
            ),
        ),
        "Services and Networking": (
            20,
            (
                "Demonstrate basic understanding of NetworkPolicies",
                "Provide and troubleshoot access to applications via services",
                "Use Ingress rules to expose applications",
            ),
        ),
    },
    "CKA": {
        "Cluster Architecture, Installation and Configuration": (
            25,
            (
                "Manage role based access control (RBAC)",
                "Prepare underlying infrastructure for installing a Kubernetes cluster",
                "Create and manage Kubernetes clusters using kubeadm",
                "Manage the lifecycle of Kubernetes clusters",
                "Implement and configure a highly-available control plane",
                "Use Helm and Kustomize to install cluster components",
                "Understand extension interfaces (CNI, CSI, CRI, etc.)",
                "Understand CRDs, install and configure operators",
            ),
        ),
        "Workloads and Scheduling": (
            15,
            (
                "Understand application deployments and how to perform rolling "
                "update and rollbacks",
                "Use ConfigMaps and Secrets to configure applications",
                "Configure workload autoscaling",
                "Understand the primitives used to create robust, self-healing, "
                "application deployments",
                "Configure Pod admission and scheduling (limits, node affinity, etc.)",
            ),
        ),
        "Services and Networking": (
            20,
            (
                "Understand connectivity between Pods",
                "Define and enforce Network Policies",
                "Use ClusterIP, NodePort, LoadBalancer service types and endpoints",
                "Use the Gateway API to manage Ingress traffic",
                "Know how to use Ingress controllers and Ingress resources",
                "Understand and use CoreDNS",
            ),
        ),
        "Storage": (
            10,
            (
                "Implement storage classes and dynamic volume provisioning",
                "Configure volume types, access modes and reclaim policies",
                "Manage persistent volumes and persistent volume claims",
            ),
        ),
        "Troubleshooting": (
            30,
            (
                "Troubleshoot clusters and nodes",
                "Troubleshoot cluster components",
                "Monitor cluster and application resource usage",
                "Manage and evaluate container output streams",
                "Troubleshoot services and networking",
            ),
        ),
    },
    "CKS": {
        "Cluster Setup": (
            15,
            (
                "Use Network security policies to restrict cluster level access",
                "Use CIS benchmark to review the security configuration of "
                "Kubernetes components (etcd, kubelet, kubedns, kubeapi)",
                "Properly set up Ingress objects with TLS",
                "Protect node metadata and endpoints",
                "Verify platform binaries before deploying",
            ),
        ),
        "Cluster Hardening": (
            15,
            (
                "Use Role Based Access Controls to minimize exposure",
                "Exercise caution in using service accounts (e.g. disable defaults, "
                "minimize permissions on newly created ones)",
                "Restrict access to Kubernetes API",
                "Upgrade Kubernetes to avoid vulnerabilities",
            ),
        ),
        "System Hardening": (
            10,
            (
                "Minimize host OS footprint (reduce attack surface)",
                "Using least-privilege identity and access management",
                "Minimize external access to the network",
                "Appropriately use kernel hardening tools such as AppArmor, seccomp",
            ),
        ),
        "Minimize Microservice Vulnerabilities": (
            20,
            (
                "Use appropriate pod security standards",
                "Manage kubernetes secrets",
                "Understand and implement isolation techniques (multi-tenancy, "
                "sandboxed containers, etc.)",
                "Implement Pod-to-Pod encryption (Cilium, Istio)",
            ),
        ),
        "Supply Chain Security": (
            20,
            (
                "Minimize base image footprint",
                "Understand your supply chain (e.g. SBOM, CI/CD, artifact repositories)",
                "Secure your supply chain (permitted registries, sign and validate "
                "artifacts, etc.)",
                "Perform static analysis of user workloads and container images "
                "(e.g. Kubesec, KubeLinter)",
            ),
        ),
        "Monitoring, Logging and Runtime Security": (
            20,
            (
                "Perform behavioral analytics to detect malicious activities",
                "Detect threats within physical infrastructure, apps, networks, "
                "data, users and workloads",
                "Investigate and identify phases of attack and bad actors within the "
                "environment",
                "Ensure immutability of containers at runtime",
                "Use Kubernetes audit logs to monitor access",
            ),
        ),
    },
}

# Drill `topic` values → curriculum domain, so coverage can be totalled.
_TOPIC_DOMAIN: dict[str, str] = {
    "Pods": "Application Design and Build",
    "Pod Design": "Application Design and Build",
    "Multi-container Pods": "Application Design and Build",
    "Jobs & CronJobs": "Application Design and Build",
    "Storage": "Application Design and Build",
    "Deployments": "Application Deployment",
    "Helm": "Application Deployment",
    "Observability": "Application Observability and Maintenance",
    "Troubleshooting": "Application Observability and Maintenance",
    "Kubernetes API": "Application Observability and Maintenance",
    "ConfigMaps": "Application Environment, Configuration and Security",
    "Secrets": "Application Environment, Configuration and Security",
    "Security": "Application Environment, Configuration and Security",
    "RBAC": "Application Environment, Configuration and Security",
    "Resource Management": "Application Environment, Configuration and Security",
    "Custom Resources": "Application Environment, Configuration and Security",
    "Services & Networking": "Services and Networking",
}


@dataclass(slots=True)
class Catalogue:
    drills: list[Drill] = field(default_factory=list)
    exams: list[Exam] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    cluster_version: str = ""

    @property
    def ids(self) -> set[str]:
        return {d.id for d in self.drills}

    def by_id(self, drill_id: str) -> Drill | None:
        for d in self.drills:
            if d.id == drill_id:
                return d
        return None

    def namespaces(self) -> set[str]:
        out: set[str] = set()
        for e in self.exams:
            for q in e.questions:
                if q.namespace:
                    out.add(q.namespace)
        return out

    def domain_counts(self, certification: str) -> Counter[str]:
        c: Counter[str] = Counter()
        for d in self.drills:
            if d.exam.upper() == certification.upper():
                c[_TOPIC_DOMAIN.get(d.topic, d.topic)] += 1
        return c


def cluster_version() -> str:
    """The server version of the cluster `KUBECONFIG` points at, e.g. `v1.35.5+k3s1`.

    Empty when kubectl cannot answer; the facts say so and the version check
    is skipped rather than guessed.
    """
    try:
        rc, out, _ = run(["kubectl", "version", "-o", "json"], timeout=20)
    except Exception:
        return ""
    if rc != 0:
        return ""
    try:
        return str(json.loads(out).get("serverVersion", {}).get("gitVersion", ""))
    except json.JSONDecodeError:
        return ""


def check_version(target: Target, cluster: str) -> None:
    """Refuse a run that asks for a Kubernetes version the test cluster cannot prove.

    A question written for 1.37 against a 1.35 cluster fails its live proof
    for reasons that have nothing to do with the question; better to stop
    before the first token than to drop every feature slot at the end.
    """
    if target.version is None or not cluster:
        return
    if minor(target.version) > minor(cluster):
        raise TargetError(
            f"version={target.version} was requested but the cluster kubiq tests "
            f"on runs {cluster}; questions for {target.version} cannot be proven "
            "there. Upgrade the cluster (k3d image in kubiq's Makefile) or ask "
            f"for {minor(cluster)[0]}.{minor(cluster)[1]}."
        )


def profile_catalogue(target: Target | None = None) -> Catalogue:
    """Read every drill and exam under the target checkout, and the cluster version."""
    t = target or current()
    cat = Catalogue(cluster_version=cluster_version())
    check_version(t, cat.cluster_version)
    for path in sorted((t.kubiq / "drills").rglob("*.yaml")):
        try:
            cat.drills.append(load_drill(path))
        except DrillError as exc:
            cat.broken.append(str(exc))
    for path in sorted((t.kubiq / "exams").glob("*.yaml")):
        try:
            cat.exams.append(load_exam(path))
        except DrillError as exc:
            cat.broken.append(str(exc))
    return cat


def render_facts(
    cat: Catalogue,
    target: Target | None = None,
    *,
    curriculum: bool = True,
    drills: bool = True,
    resumed: list[dict[str, Any]] | None = None,
) -> str:
    """The facts block injected into a prompt.

    The drill list is ~3k tokens and the curriculum ~1k, paid on every
    dispatch, so each role gets only what it uses: the author both (de-dup
    and scope), the commander both (planning), the critic the curriculum (it
    de-dups with `similar_drills`, which is exact), the tester neither.
    """
    t = target or current()
    lines = [
        "<facts>",
        f"Kubiq checkout: {t.kubiq}",
        f"Exam to author: {t.exam} ({t.certification}, {t.questions} questions)",
        f"Kubiq server for testing: {t.server}",
        f"Test cluster: {cat.cluster_version or 'unknown (kubectl did not answer)'}",
        "Kubernetes version to author against: "
        + (t.version or "not specified; use the cluster's version"),
        f"Existing drills: {len(cat.drills)} across {len(cat.exams)} exams",
        "Namespaces other exams already use (plan_exam refuses them; choose a "
        "unique namespace): "
        + (
            ", ".join(
                sorted(
                    {
                        ns
                        for e in cat.exams
                        if e.id != t.exam
                        for q in e.questions
                        if (ns := q.namespace)
                    }
                )
            )
            or "none"
        ),
        "",
        f"{t.certification} curriculum (CNCF): domain, share of the exam, and the "
        "competencies it covers. Every question must exercise one of these "
        "competencies; nothing outside this list belongs in the exam.",
    ]
    if curriculum:
        for dom, (pct, skills) in CURRICULUM.get(t.certification, {}).items():
            have = cat.domain_counts(t.certification).get(dom, 0)
            lines.append(f"  - {dom}: {pct}%  (existing drills in this domain: {have})")
            lines.extend(f"      * {skill}" for skill in skills)
    else:
        del lines[-2:]
    if drills:
        lines.append("")
        lines.append(
            "Existing drills (id · topic · title) — an exam must not repeat these:"
        )
        own = {q.drill for e in cat.exams if e.id == t.exam for q in e.questions}
        for d in sorted(cat.drills, key=lambda d: (d.exam, d.order)):
            if d.id not in own:  # this exam's own questions are staged, not "existing"
                lines.append(f"  - {d.id} · {d.topic} · {d.title}")
    if resumed:
        lines.append("")
        lines.append(
            "RESUMED from an earlier run of this exam: these questions are already "
            "staged at these numbers. Do not re-author them; plan the remaining "
            "numbers around them. The state after each is the persisted record, "
            "valid while the drill and namespace are unchanged: CONFIRMED means "
            "proven and ruled confirmed — nothing to do, it is ready for "
            "finalize_exam; PROVEN means proven but not yet ruled — send it to a "
            "critic, never to prove_questions; REFUTED/UNPROVEN means the critic "
            "sent it back — a fix round; unproven means it needs a proof."
        )
        for r in resumed:
            ruling = str(r.get("ruling") or "")
            state = (
                ruling.upper()
                if ruling and r.get("proven")
                else "PROVEN"
                if r.get("proven")
                else "unproven"
            )
            lines.append(
                f"  - {r['question']}. {r['drill']} · {r['topic']} · {r['title']} "
                f"· weight {r['weight']} · ns {r['namespace']} · {state}"
            )
        broken = [r for r in resumed if r.get("problem")]
        if broken:
            lines.append("")
            lines.append(
                "These resumed questions FAIL today's validation or the API server's "
                "dry-run. Before any proof, dispatch author for each with the problem "
                "quoted verbatim (one author per question, in one message):"
            )
            for r in broken:
                lines.append(f"  - {r['question']}. {r['drill']}: {r['problem']}")
    if cat.broken:
        lines.append("")
        lines.append("Files kubiq would refuse to load (fix before adding more):")
        lines.extend(f"  - {b}" for b in cat.broken[:10])
    lines.append("</facts>")
    return "\n".join(lines)


def banner(cat: Catalogue, target: Target | None = None) -> str:
    t = target or current()
    return (
        f"{t.kubiq.name}: {len(cat.drills)} drills in {len(cat.exams)} exams; "
        f"authoring {t.exam} ({t.certification}, {t.questions} questions) "
        f"against {t.server}"
    )
