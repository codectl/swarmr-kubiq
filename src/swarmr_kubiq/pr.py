"""Landing a written exam in the kubiq checkout: branch, commit, pull request.

One responsibility: the git and `gh` choreography after the files exist on
disk. Nothing in here decides what to write or whether it is proven; it takes
the paths the run wrote and puts exactly those on a branch.

The checkout is the user's. The commit may contain only the paths this run
wrote, so a dirty file outside that set is a refusal, never a sweep: the one
thing worse than no PR is a PR that carries half-finished work the user never
meant to publish. `gh` is optional — without it (or without a login) the result
carries the push command instead of a URL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from swarmr.core.team import TeamError

from swarmr_kubiq.target import Target

__all__ = ["PrError", "Run", "Runner", "branch_name", "commit_message", "open_pr"]


class PrError(TeamError):
    """The exam could not be committed or filed; the message says why."""


@dataclass(frozen=True, slots=True)
class Run:
    """What a command returned; the shape the runner must produce."""

    code: int
    out: str
    err: str = ""


Runner = Callable[[list[str], Path], Run]


def _run(argv: list[str], cwd: Path) -> Run:
    """Run `argv` in `cwd` and capture; never raises on a non-zero exit."""
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)
    return Run(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def branch_name(exam: str) -> str:
    """The branch an exam lands on: `exam/<exam>`."""
    return f"exam/{exam}"


def commit_message(exam: str, n_questions: int) -> str:
    """The commit subject and a one-line body naming the question count."""
    plural = "question" if n_questions == 1 else "questions"
    body = f"{n_questions} {plural}, each proven on a live kubiq session."
    return f"feat: add {exam}\n\n{body}"


def _git(run: Runner, cwd: Path, *args: str) -> Run:
    result = run(["git", *args], cwd)
    if result.code != 0:
        raise PrError(f"git {args[0]} failed in {cwd}: {result.err or result.out}")
    return result


def _relative(target: Target, written: list[Path]) -> list[str]:
    rel: list[str] = []
    for path in written:
        try:
            rel.append(path.resolve().relative_to(target.kubiq.resolve()).as_posix())
        except ValueError as exc:
            raise PrError(f"{path} is outside the kubiq checkout {target.kubiq}") from exc
    return sorted(set(rel))


def _foreign_changes(status: str, allowed: list[str]) -> list[str]:
    """Porcelain lines naming paths that are neither written nor inside a written dir."""
    foreign: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed):
            continue
        foreign.append(path)
    return foreign


def _gh_available(run: Runner, cwd: Path) -> bool:
    if shutil.which("gh") is None:
        return False
    return run(["gh", "auth", "status"], cwd).code == 0


def open_pr(
    target: Target,
    written: list[Path],
    title: str,
    body: str,
    *,
    run: Runner = _run,
) -> dict[str, str | None]:
    """Commit `written` on `exam/<exam>` in the checkout and open a PR when `gh` can.

    Returns {branch, commit, pr_url} when the PR was created, otherwise
    {branch, commit, pr_url: None, push: "<git push command>"}. Raises
    `PrError` when nothing was written, a path lies outside the checkout, or
    the checkout holds changes to files outside `written`.
    """
    if not written:
        raise PrError("nothing to commit: no files were written")
    cwd = target.kubiq
    paths = _relative(target, written)

    status = _git(run, cwd, "status", "--porcelain", "--untracked-files=all").out
    if foreign := _foreign_changes(status, paths):
        listed = ", ".join(foreign[:5]) + (" …" if len(foreign) > 5 else "")
        raise PrError(
            f"the checkout has uncommitted changes outside the exam ({listed}); "
            "commit or stash them first so they are not swept into the exam commit"
        )

    branch = branch_name(target.exam)
    exists = run(["git", "rev-parse", "--verify", "--quiet", branch], cwd).code == 0
    _git(run, cwd, "checkout", *(() if exists else ("-b",)), branch)
    _git(run, cwd, "add", "--", *paths)
    n_questions = sum(p.startswith("drills/") for p in paths)
    _git(run, cwd, "commit", "-m", commit_message(target.exam, n_questions))
    commit = _git(run, cwd, "rev-parse", "--short", "HEAD").out

    result: dict[str, str | None] = {"branch": branch, "commit": commit, "pr_url": None}
    if _gh_available(run, cwd):
        argv = ["gh", "pr", "create", "--base", "main", "--head", branch]
        pr = run([*argv, "--title", title, "--body", body], cwd)
        if pr.code != 0:
            raise PrError(f"gh pr create failed: {pr.err or pr.out}")
        result["pr_url"] = pr.out.splitlines()[-1].strip() if pr.out else ""
    else:
        result["push"] = f"git push -u origin {branch}"
    return result
