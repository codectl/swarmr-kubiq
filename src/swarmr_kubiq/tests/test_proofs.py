"""The on-disk proof cache: keyed by what was proven, tolerant of a bad file."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from swarmr_kubiq import proofs
from swarmr_kubiq.drill import Check, Drill
from swarmr_kubiq.target import Target


def _drill(**overrides: object) -> Drill:
    base = Drill(
        id="pod-basics",
        order=701,
        exam="CKAD",
        title="Run a Pod",
        topic="Application Design and Build",
        task="Create a Pod named `web` running `nginx:alpine`.",
        solution="kubectl run web --image=nginx:alpine",
        checks=[
            Check(
                name="web runs nginx",
                resource={"kind": "Pod", "name": "web"},
                assert_=[{"path": "spec.containers[0].image", "contains": "nginx"}],
            )
        ],
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _target(checkout: Path, exam: str = "ckad-mock-7") -> Target:
    return Target(
        kubiq=checkout,
        exam=exam,
        certification="CKAD",
        server="http://localhost:8080",
        questions=21,
    )


@pytest.fixture
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(home))
    return home


def test_key_is_stable_for_the_same_drill_and_namespace() -> None:
    assert proofs.proof_key(_drill(), "ckad01-pod") == proofs.proof_key(
        _drill(), "ckad01-pod"
    )
    assert len(proofs.proof_key(_drill(), None)) == 64


def test_key_changes_with_namespace_task_or_solution() -> None:
    base = proofs.proof_key(_drill(), "ckad01-pod")
    assert proofs.proof_key(_drill(), "ckad02-pod") != base
    assert proofs.proof_key(_drill(), None) != base
    assert (
        proofs.proof_key(_drill(task="Create a Pod named `api`."), "ckad01-pod") != base
    )
    assert proofs.proof_key(
        _drill(solution="kubectl run api --image=x"), "ckad01-pod"
    ) != (base)


def test_key_changes_with_the_proof_rule_version(monkeypatch) -> None:
    """A verdict recorded under an older meaning of `proven` must never be
    found again: proof and the ruling stored with it vanish together."""
    now = proofs.proof_key(_drill(), "ckad01-pod")
    monkeypatch.setattr(proofs, "PROOF_SCHEMA", proofs.PROOF_SCHEMA - 1)
    assert proofs.proof_key(_drill(), "ckad01-pod") != now


def test_cache_path_lives_under_xdg_cache_home_per_checkout_and_exam(
    cache_home: Path, tmp_path: Path
) -> None:
    one = proofs.cache_path(_target(tmp_path / "kubiq-a"))
    two = proofs.cache_path(_target(tmp_path / "kubiq-b"))
    assert one.parent.parent == cache_home / "swarmr-kubiq"
    assert one.name == "ckad-mock-7.json"
    assert one != two, "two checkouts never share a cache"
    assert len(one.parent.name) == 12
    assert (
        proofs.cache_path(_target(tmp_path / "kubiq-a", "cka-mock-1")).parent
        == one.parent
    )


def test_cache_path_defaults_to_dot_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = proofs.cache_path(_target(tmp_path / "kubiq"))
    assert path.parent.parent == tmp_path / ".cache" / "swarmr-kubiq"


def test_save_then_load_round_trips_and_keeps_other_proofs(
    cache_home: Path, tmp_path: Path
) -> None:
    target = _target(tmp_path / "kubiq")
    assert proofs.load_proofs(target) == {}
    first = proofs.proof_key(_drill(), "ckad01-pod")
    second = proofs.proof_key(_drill(id="svc"), "ckad02-svc")
    proofs.save_proof(target, first, {"kind": "verdict", "proven": True, "question": 0})
    proofs.save_proof(target, second, {"kind": "verdict", "proven": False, "question": 1})
    proofs.save_proof(
        target, first, {"kind": "verdict", "proven": True, "question": 0, "r": 2}
    )
    loaded = proofs.load_proofs(target)
    assert loaded == {
        first: {"kind": "verdict", "proven": True, "question": 0, "r": 2},
        second: {"kind": "verdict", "proven": False, "question": 1},
    }
    assert proofs.cache_path(target).is_file()


def test_a_corrupt_or_foreign_file_is_an_empty_cache(
    cache_home: Path, tmp_path: Path
) -> None:
    target = _target(tmp_path / "kubiq")
    path = proofs.cache_path(target)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert proofs.load_proofs(target) == {}
    path.write_text('["a", "list"]')
    assert proofs.load_proofs(target) == {}
    path.write_text('{"k": "not a verdict", "ok": {"proven": true}}')
    assert proofs.load_proofs(target) == {"ok": {"proven": True}}
    proofs.save_proof(target, "new", {"proven": False})
    assert proofs.load_proofs(target) == {
        "ok": {"proven": True},
        "new": {"proven": False},
    }
