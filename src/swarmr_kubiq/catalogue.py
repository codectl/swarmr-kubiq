"""What already exists in the kubiq checkout: every drill, every exam.

One responsibility: profiling the target before the first token, and rendering
that profile as the `facts` block every prompt receives. The author must not
repeat a question that exists, the critic must be able to compare against the
whole catalogue, and the commander must know which topics the curriculum still
lacks — all of that comes from here, read once at startup.

Mirrors `discovery.py` in the sibling teams. Every call is a read.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from swarmr_kubiq.drill import Drill, DrillError, Exam, load_drill, load_exam
from swarmr_kubiq.target import Target, current

__all__ = [
    "CURRICULUM",
    "Catalogue",
    "banner",
    "profile_catalogue",
    "render_facts",
]

# Domain weights of the real exams, as published by the CNCF. The commander
# plans slots against these so an exam is balanced the way the real one is.
CURRICULUM: dict[str, dict[str, int]] = {
    "CKAD": {
        "Application Design and Build": 20,
        "Application Deployment": 20,
        "Application Observability and Maintenance": 15,
        "Application Environment, Configuration and Security": 25,
        "Services and Networking": 20,
    },
    "CKA": {
        "Cluster Architecture, Installation and Configuration": 25,
        "Workloads and Scheduling": 15,
        "Services and Networking": 20,
        "Storage": 10,
        "Troubleshooting": 30,
    },
    "CKS": {
        "Cluster Setup": 15,
        "Cluster Hardening": 15,
        "System Hardening": 10,
        "Minimize Microservice Vulnerabilities": 20,
        "Supply Chain Security": 20,
        "Monitoring, Logging and Runtime Security": 20,
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


def profile_catalogue(target: Target | None = None) -> Catalogue:
    """Read every drill and exam under the target checkout."""
    t = target or current()
    cat = Catalogue()
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


def render_facts(cat: Catalogue, target: Target | None = None) -> str:
    """The facts block injected into every prompt."""
    t = target or current()
    lines = [
        "<facts>",
        f"Kubiq checkout: {t.kubiq}",
        f"Exam to author: {t.exam} ({t.certification}, {t.questions} questions)",
        f"Kubiq server for testing: {t.server}",
        f"Existing drills: {len(cat.drills)} across {len(cat.exams)} exams",
        "",
        "Curriculum domains and target share (percent of weight):",
    ]
    for dom, pct in CURRICULUM.get(t.certification, {}).items():
        have = cat.domain_counts(t.certification).get(dom, 0)
        lines.append(f"  - {dom}: {pct}%  (existing drills in this domain: {have})")
    lines.append("")
    lines.append("Existing drills (id · topic · title) — an exam must not repeat these:")
    for d in sorted(cat.drills, key=lambda d: (d.exam, d.order)):
        lines.append(f"  - {d.id} · {d.topic} · {d.title}")
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
