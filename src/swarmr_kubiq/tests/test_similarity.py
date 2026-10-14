"""Near-duplicate detection: renamed copies score high, other mechanics low."""

from __future__ import annotations

from swarmr_kubiq.catalogue import Catalogue
from swarmr_kubiq.drill import Drill, parse_drill
from swarmr_kubiq.similarity import (
    THRESHOLD,
    fingerprint,
    is_duplicate,
    nearest,
    score,
)

RUN_EXPOSE = """
id: run-and-expose
order: 1
exam: CKAD
title: Run a Pod and expose it
topic: Services & Networking
task: |
  Create a Pod `ckad01-pod` with image `nginx:alpine` in this namespace and expose
  it with a ClusterIP Service `ckad01-svc` on port `80`.
solution: |
  kubectl run ckad01-pod --image=nginx:alpine --port=80
  kubectl expose pod ckad01-pod --name=ckad01-svc --port=80
checks:
  - name: pod runs nginx
    resource: { kind: Pod, name: ckad01-pod }
    assert:
      - { path: "spec.containers[0].image", equals: nginx:alpine }
  - name: service selects the pod
    resource: { kind: Service, name: ckad01-svc }
    assert:
      - { path: "spec.ports[*].port", equals: 80 }
      - { path: spec.selector.run, equals: ckad01-pod }
"""

RUN_EXPOSE_RENAMED = """
id: pod-plus-service
order: 2
exam: CKAD
title: Start a web Pod and put a Service in front
topic: Services & Networking
task: |
  Run a Pod named `foo-bar-aecs` using `nginx:1.27` and create a Service
  `foo-bar-aecs-svc` of type ClusterIP that routes port `8080` to it.
solution: |
  kubectl run foo-bar-aecs --image=nginx:1.27 --port=80
  kubectl expose pod foo-bar-aecs --name=foo-bar-aecs-svc --port=8080 --target-port=80
checks:
  - name: pod image
    resource: { kind: Pod, name: foo-bar-aecs }
    assert:
      - { path: "spec.containers[0].image", equals: nginx:1.27 }
  - name: service
    resource: { kind: Service, name: foo-bar-aecs-svc }
    assert:
      - { path: "spec.ports[0].port", equals: 8080 }
      - { path: "spec.ports[0].targetPort", equals: 80 }
      - { path: spec.selector.run, equals: foo-bar-aecs }
"""

HEADLESS = """
id: headless-service
order: 3
exam: CKAD
title: Headless Service for a StatefulSet
topic: Services & Networking
task: |
  A StatefulSet `db-aecs` runs in this namespace. Create a headless Service
  `db-aecs-hl` so each replica gets a stable DNS record.
setup: |
  apiVersion: apps/v1
  kind: StatefulSet
  metadata:
    name: db-aecs
  spec:
    serviceName: db-aecs-hl
    selector: { matchLabels: { app: db } }
    template:
      metadata: { labels: { app: db } }
      spec:
        containers: [{ name: redis, image: redis:7 }]
solution: |
  kubectl create service clusterip db-aecs-hl --clusterip=None --tcp=6379
  kubectl patch svc db-aecs-hl -p '{"spec":{"selector":{"app":"db"}}}'
checks:
  - name: headless
    resource: { kind: Service, name: db-aecs-hl }
    assert:
      - { path: spec.clusterIP, equals: None }
      - { path: spec.selector.app, equals: db }
"""


def _drill(text: str) -> Drill:
    return parse_drill(text)


def test_fingerprint_strips_names_indices_and_read_verbs() -> None:
    fp = fingerprint(_drill(RUN_EXPOSE))
    assert fp.kinds == {"pod", "service"}
    assert fp.verbs == {"run", "expose"}
    assert "spec.containers.image" in fp.paths
    assert "spec.ports.port" in fp.paths
    assert not any("[" in p for p in fp.paths)
    assert {"pod", "service", "expose"} <= fp.tokens
    assert not any("-" in t or t.isdigit() for t in fp.tokens)
    assert "the" not in fp.tokens

    fp2 = fingerprint(_drill(HEADLESS))
    assert "statefulset" in fp2.kinds  # from the `kind:` line in setup
    assert fp2.verbs == {"create", "patch"}


def test_renamed_copy_is_a_duplicate() -> None:
    s = score(_drill(RUN_EXPOSE), _drill(RUN_EXPOSE_RENAMED))
    assert s >= THRESHOLD
    assert is_duplicate(s)


def test_other_mechanic_in_same_domain_is_not() -> None:
    s = score(_drill(RUN_EXPOSE), _drill(HEADLESS))
    assert s < THRESHOLD
    assert not is_duplicate(s)
    assert score(_drill(RUN_EXPOSE_RENAMED), _drill(HEADLESS)) < THRESHOLD


def test_score_is_symmetric_and_bounded() -> None:
    a, b, c = (_drill(t) for t in (RUN_EXPOSE, RUN_EXPOSE_RENAMED, HEADLESS))
    for x, y in ((a, b), (a, c), (b, c)):
        assert score(x, y) == score(y, x)
        assert 0.0 <= score(x, y) <= 1.0
    assert score(a, a) == 1.0


def test_nearest_ranks_duplicate_first_with_reason() -> None:
    a, b, c = (_drill(t) for t in (RUN_EXPOSE, RUN_EXPOSE_RENAMED, HEADLESS))
    cat = Catalogue(drills=[c, b, a])
    ranked = nearest(a, cat)
    assert [r[0] for r in ranked] == ["pod-plus-service", "headless-service"]
    _, top_score, reason = ranked[0]
    assert top_score >= THRESHOLD
    assert "spec.ports.port" in reason
    assert "pod" in reason and "expose" in reason
    assert ranked[1][1] < THRESHOLD
    assert nearest(a, cat, n=1) == ranked[:1]
    assert nearest(a, Catalogue()) == []
