"""Persisted proofs: a rerun does not re-prove a question that has not changed.

A proof costs a session, a solution and a grade — minutes per question — and
the verdict used to live only in the tools' process memory, so a run that was
interrupted or restarted proved all of the exam again. This module keeps every
verdict on disk under a key that is the content of what was proven: the drill
exactly as staged (`dump_drill`) and the namespace it ran in. Any edit to the
drill — task, solution, checks, setup — or a change of namespace yields a new
key, so a cached verdict can never vouch for a different question.

Location: `$XDG_CACHE_HOME/swarmr-kubiq/<checkout>/<exam>.json` (default
`~/.cache`), where `<checkout>` is the first twelve hex digits of the SHA-256
of the kubiq checkout path, so two checkouts authoring the same exam id do not
share a cache. The file is a JSON object from key to the verdict's `to_json`
payload. A missing or unreadable file is an empty cache, never an error.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from swarmr_kubiq.drill import Drill, dump_drill
from swarmr_kubiq.target import Target

__all__ = ["PROOF_SCHEMA", "cache_path", "load_proofs", "proof_key", "save_proof"]

# Bumped whenever what "proven" means changes, so a verdict recorded under an
# older rule can never vouch for a question under the new one; old entries
# simply miss and the question is proved again.
#   1  question-level negative control
#   2  every check must fail on the untouched namespace
PROOF_SCHEMA = 2


def proof_key(drill: Drill, namespace: str | None) -> str:
    """SHA-256 of the proof rule version, the staged drill text and its
    namespace; the proof's identity."""
    material = f"v{PROOF_SCHEMA}\0" + dump_drill(drill) + "\0" + (namespace or "")
    return hashlib.sha256(material.encode()).hexdigest()


def cache_path(target: Target) -> Path:
    """Where this checkout's proofs for this exam are kept."""
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    checkout = hashlib.sha256(str(target.kubiq).encode()).hexdigest()[:12]
    return root / "swarmr-kubiq" / checkout / f"{target.exam}.json"


def load_proofs(target: Target) -> dict[str, dict[str, Any]]:
    """Every saved verdict by proof key; `{}` when the file is missing or corrupt."""
    try:
        data = json.loads(cache_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def save_proof(target: Target, key: str, verdict: dict[str, Any]) -> None:
    """Record one verdict under its key, keeping every other saved proof."""
    path = cache_path(target)
    proofs = load_proofs(target)
    proofs[key] = verdict
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proofs, indent=1, sort_keys=True), encoding="utf-8")
