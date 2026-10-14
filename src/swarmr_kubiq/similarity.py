"""Is this question already in the catalogue, under another name?

One responsibility: an embedding-free similarity between two drills, so the
author can check a draft against every existing drill before the tester spends
a cluster session on it. Names, ports and namespaces are deliberately ignored —
`kubectl run web` + `kubectl expose` with new names is the same question — and
the mechanic is weighted over the wording: what the checks assert and which
kinds and verbs the solution touches matter more than how the task is phrased.

Pure functions over `Drill`; nothing here reads disk or network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from swarmr_kubiq.catalogue import Catalogue
from swarmr_kubiq.drill import Drill

__all__ = ["THRESHOLD", "Fingerprint", "fingerprint", "is_duplicate", "nearest", "score"]

# Above this two drills are the same question. Chosen so renamed copies of the
# same mechanic land above it and different mechanics in one domain stay below.
THRESHOLD = 0.62

_WEIGHTS = {"paths": 0.45, "mechanic": 0.35, "tokens": 0.20}

_KIND_LINE = re.compile(r"^\s*-?\s*kind:\s*['\"]?([A-Za-z]+)", re.MULTILINE)
_CLI = re.compile(r"\b(kubectl|k|helm)\s+([a-z-]+)(?:\s+([a-z]+))?")
# kubectl verbs that only read state: present in nearly every solution, no signal.
_READ_VERBS = {"get", "describe", "config", "explain", "api-resources", "version"}
# Resource nouns kubectl accepts after a verb, with their Kind.
_NOUNS = {
    "pod": "Pod",
    "pods": "Pod",
    "po": "Pod",
    "deployment": "Deployment",
    "deploy": "Deployment",
    "deployments": "Deployment",
    "service": "Service",
    "svc": "Service",
    "services": "Service",
    "configmap": "ConfigMap",
    "cm": "ConfigMap",
    "secret": "Secret",
    "job": "Job",
    "cronjob": "CronJob",
    "cj": "CronJob",
    "ingress": "Ingress",
    "ing": "Ingress",
    "namespace": "Namespace",
    "ns": "Namespace",
    "serviceaccount": "ServiceAccount",
    "sa": "ServiceAccount",
    "role": "Role",
    "rolebinding": "RoleBinding",
    "clusterrole": "ClusterRole",
    "clusterrolebinding": "ClusterRoleBinding",
    "networkpolicy": "NetworkPolicy",
    "netpol": "NetworkPolicy",
    "pvc": "PersistentVolumeClaim",
    "persistentvolumeclaim": "PersistentVolumeClaim",
    "pv": "PersistentVolume",
    "persistentvolume": "PersistentVolume",
    "quota": "ResourceQuota",
    "resourcequota": "ResourceQuota",
    "limitrange": "LimitRange",
    "statefulset": "StatefulSet",
    "sts": "StatefulSet",
    "daemonset": "DaemonSet",
    "ds": "DaemonSet",
    "crd": "CustomResourceDefinition",
}

_INDEX = re.compile(r"\[[^\]]*\]")
_QUOTED = re.compile(r"`[^`]*`|'[^']*'|\"[^\"]*\"")
_WORD = re.compile(r"[a-z][a-z-]+")
_STOP = frozenset(
    _WORD.findall(
        "a an and are as at be been but by can create created do does for from has "
        "have in into is it its must of on or so than that the then there these this "
        "those to use using via which will with within you your part all also already "
        "any each only new one two currently running runs named name called exists "
        "existing should need needs ensure make sure such their when where"
    )
)


@dataclass(slots=True)
class Fingerprint:
    kinds: set[str] = field(default_factory=set)
    verbs: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)

    @property
    def mechanic(self) -> set[str]:
        return {f"kind:{k}" for k in self.kinds} | {f"verb:{v}" for v in self.verbs}


def _norm_path(path: str) -> str:
    return _INDEX.sub("", path).strip(".")


def _tokens(task: str) -> set[str]:
    text = _QUOTED.sub(" ", task.lower())
    out: set[str] = set()
    for w in _WORD.findall(text):
        # Hyphenated words are names (`ckad01-pod`, `foo-bar-aecs`), not vocabulary.
        if "-" in w or len(w) < 3 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def fingerprint(drill: Drill) -> Fingerprint:
    """What a drill is about, with every name and number stripped."""
    fp = Fingerprint(tokens=_tokens(drill.task))
    manifests = "\n".join([drill.setup or "", drill.solution, *drill.files.values()])
    fp.kinds.update(k.lower() for k in _KIND_LINE.findall(manifests))
    for _, verb, noun in _CLI.findall(drill.solution):
        if verb not in _READ_VERBS:
            fp.verbs.add(verb)
        if noun in _NOUNS:
            fp.kinds.add(_NOUNS[noun].lower())
    for check in drill.checks:
        if check.resource and isinstance(check.resource.get("kind"), str):
            fp.kinds.add(check.resource["kind"].lower())
        for a in check.assert_:
            if isinstance(a.get("path"), str):
                fp.paths.add(_norm_path(a["path"]))
    return fp


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def score(a: Drill, b: Drill) -> float:
    """Similarity in [0, 1]; symmetric; components neither drill has are ignored."""
    fa, fb = fingerprint(a), fingerprint(b)
    pairs = {
        "paths": (fa.paths, fb.paths),
        "mechanic": (fa.mechanic, fb.mechanic),
        "tokens": (fa.tokens, fb.tokens),
    }
    total = sum(_WEIGHTS[k] for k, (x, y) in pairs.items() if x or y)
    if not total:
        return 0.0
    got = sum(_WEIGHTS[k] * _jaccard(x, y) for k, (x, y) in pairs.items())
    return round(got / total, 4)


def is_duplicate(value: float) -> bool:
    return value >= THRESHOLD


def _reason(a: Fingerprint, b: Fingerprint) -> str:
    parts: list[str] = []
    for label, x, y in (
        ("paths", a.paths, b.paths),
        ("kinds", a.kinds, b.kinds),
        ("verbs", a.verbs, b.verbs),
    ):
        if shared := sorted(x & y):
            parts.append(f"{label}: {', '.join(shared)}")
    if not parts and (words := sorted(a.tokens & b.tokens)):
        parts.append(f"task wording only: {', '.join(words[:6])}")
    return "; ".join(parts) or "no overlap"


def nearest(
    drill: Drill, catalogue: Catalogue, n: int = 5
) -> list[tuple[str, float, str]]:
    """The `n` catalogue drills closest to `drill`: (id, score, why), best first."""
    fp = fingerprint(drill)
    ranked = [
        (other.id, score(drill, other), _reason(fp, fingerprint(other)))
        for other in catalogue.drills
        if other.id != drill.id
    ]
    ranked.sort(key=lambda r: (-r[1], r[0]))
    return ranked[:n]
