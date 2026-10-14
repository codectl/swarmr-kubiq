# CLAUDE.md — swarmr-kubiq

## Overview

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq) — drill files under `drills/<exam>/` and the manifest `exams/<exam>.yaml` — and proves every question on a live kubiq session before it opens a pull request. A commander delegates to an author (drafts questions against the existing catalogue), a tester (runs each drill through a real candidate shell) and a critic (tries to disprove the tester's verdict); nothing reaches the PR without a verdict.

Mutating by design, but only in two places: the kubiq checkout (files under the exam's own paths, committed on `exam/<exam>`) and the running kubiq (sessions the harness itself created). The user's other work is never swept into the commit — `pr.py` refuses on a dirty checkout — and a session the harness did not create is never deleted. Nothing about the catalogue is hardcoded — `catalogue.py` profiles the checkout at build time and the profile is injected into every prompt.

- **Distribution:** `swarmr-kubiq` (`src/swarmr_kubiq`, hatchling)
- **Python:** >=3.13; `git`, `kubectl` and `docker compose` on PATH; `gh` optional
- **Deps:** `swarmr>=1.3,<2`, `pyyaml`, `httpx`
- **Entry point:** `swarmr.teams` group → `exam_author = "swarmr_kubiq:TEAM"`
- **Script:** none

**The roster.** `commander` orchestrates and holds no catalogue or cluster tools of its own. `author` drafts drills and the exam manifest. `tester` proves each drill on a live session. `critic` re-reads the tester's evidence and tries to refute it; its word is the verdict.

## Entry Points

### The team (`__init__.py` → `TEAM`)

Reached through `swarmr`'s surfaces, not its own:

```
teams --target exam_author --kubiq ../kubiq --exam ckad-mock-7
teams exam_author --kubiq ../kubiq --exam ckad-mock-7 "CKAD, 21 questions, include 1.37 features"
```

Over MCP the tool is `start_exam_author(request, kubiq, exam, certification?, server?, questions?)`.

Importing this module must stay cheap: it is what `teams --list` and the MCP server read to publish the team. `build`, `profile` and `render_report` are declared with `Lazy`; only `digest`, `is_error` and `target.PARAMS` are imported directly.

**Per-call arguments** (`target.PARAMS`): `kubiq` (required; checkout root holding `drills/` and `exams/`), `exam` (required; must not exist yet), `certification` (CKAD default, CKA, CKS), `server` (running kubiq, default `http://localhost:8080`), `questions` (21, or 16 for CKS). **Nothing about the target comes from the environment.**

**Environment (tuning only):** `KUBECONFIG` (the cluster kubiq provisions sessions on; `kubectl exec` into `cand-<session>` uses it), `KUBIQ_SETTLE_SECONDS` (wait after a kubiq restart before the first session), `LODESTAR_URL` (lodestar MCP endpoint for "what's new" docs; unset → static feature list). Model credentials come from `swarmr` (`KIMI_*`).

## Architecture

**Team declaration**\
`__init__.py` — the `Team` object: name `exam_author`, summary, routing description, vocabulary (`orchestrator="commander"`, `audit_agents=("critic",)`, `report_tool="file_exam_report"`, `digest`, `is_error`), roster, `params`, and the `Lazy` targets.

**Assembly**\
`agent.py` — `build(run)` and `profile_target(run)`. Both begin with `target.activate(run.params)`. Stock Deep Agents: one `create_deep_agent` commander plus three `SubAgent` specialists reached through the built-in `task` tool. Holds the filesystem permission rule sets.

**Target**\
`target.py` — `PARAMS`, `Target(kubiq, exam, certification, server, questions)` with `.exam_file`/`.drill_dir`, `activate(params)` (validates the checkout root, the exam id shape, that the exam does not exist yet; sets the ContextVar), `current()` (`TargetError(TeamError)` outside a run).

**Drill schema**\
`drill.py` — `Drill`, `Check`, `Exam`, `Question` dataclasses mirroring kubiq's `src/drill.rs`; `parse_drill` / `validate_drill` / `dump_drill` / `load_drill` and the exam counterparts; `DrillError`. Validation is the only place the format's rules live (ids, order, weights, check shapes, the `/opt/student-node/start.d/<id>.sh` hook convention).

**Catalogue**\
`catalogue.py` — `profile_catalogue()` reads every drill and exam in the checkout: ids, titles, topics, task texts, namespaces, broken files; `render_facts()` and `banner()` turn it into prompt text; `CURRICULUM` holds the per-certification domain weights.

**Cluster side**\
`kubiq_api.py` — HTTP client for a running kubiq: list exams, create session, wait ready, exec in the candidate shell (`kubectl exec -n kubiq-sessions cand-<id>`), check a question, finish, delete. Refuses to touch a session it did not create. `harness.py` — `test_drill`: write the candidate files into the checkout, restart kubiq, then negative control → solve → check → end → teardown proof; returns a structured verdict with the grader's detail.

**Uniqueness and docs**\
`similarity.py` — token-overlap similarity of task texts and check paths against the catalogue; nearest neighbours with scores. `docs.py` — lodestar MCP client (`search_docs`, `get_page`) with a static fallback.

**Prompts**\
`prompts.py` — `commander`, `author`, `tester`, `critic`, the shared `CONTRACT`, `SWEEP_REQUEST`. No catalogue fact appears here; everything target-specific arrives as `facts`.

**Tools**\
`tools.py` — the `@tool`s per role: `AUTHOR_TOOLS`, `TESTER_TOOLS`, `CRITIC_TOOLS`; the commander holds the report tool only.

**Result shaping**\
`output.py` — `emit` (byte cap), `guard` (exception containment; a failure is text starting `tool error:`). `digest.py` — `digest_result`, `is_tool_error`.

**Report and PR**\
`report_tool.py` — `file_exam_report` tool and `render_report_args`: header with counts and confirmed weight, one line per question (`NN. <title> — <weight> pts — <verdict> (rounds)`), new-feature coverage, branch/commit with the `git push` and `gh pr create` lines, caveats; renders with any keys missing. `pr.py` — `open_pr(target, written, title, body)`: `git status --porcelain` refusal on foreign dirty files, `exam/<exam>` branch (created or switched to), `git add` of exactly the written paths, `feat: add <exam>` commit, `gh pr create` when `gh` is logged in, else the push hint. The runner is injectable (`run=`) so tests drive it with a fake. `PrError(TeamError)`.

## Roles and Tool Sets

|Role|Tools|Permissions|
|---|---|---|
|`commander`|`file_exam_report`|`_NO_WRITES`|
|`author`|catalogue reads, similarity, docs search, drill write/validate|exam paths only|
|`tester`|harness (restart, session, exec, check, finish, teardown proof)|exam paths only|
|`critic`|catalogue reads, similarity, harness reads|`_NO_WRITES`|

**Only the tester creates sessions.** The critic reads the tester's evidence and may re-run a check, but never drafts or rewrites a drill; the author never runs the harness. A verdict is the critic's word: `confirmed`, `refuted` or `unproven`.

**The commander holds no catalogue or cluster tools by design:** its context stays clean, and it cannot fabricate an observation it never received. The one tool it holds files the report.

## Flow

```
author (draft N) → tester (prove each) ⇄ author (fix solution, never the task) → critic → pr → report
```

A refuted question goes back to the author with the grader's detail; the task text and checks are frozen once the critic has accepted the draft, only the solution and setup may change. `rounds` in the report counts those trips.

## Testing

No model calls, no cluster, no `docker`. `testpaths = ["src"]`, tests beside the code they cover, excluded from the wheel. Fixtures are temp kubiq-like trees and real drills copied from the kubiq checkout; `pr.py` is driven with a fake runner; `harness.py` with a fake kubiq API.

```
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ../swarmr -e ".[dev]"
pytest
ruff check . && pyright
```

This project pins `venvPath="."`/`venv=".venv"`: that is the only project-level signal an editor's pyright reads. CI builds `.venv` too, so the one path is correct everywhere.

## Dependencies

- `swarmr>=1.3,<2` — `Team`, `Member`, `Param`, `RunContext`, `TeamBuild`, `Lazy`, `TeamError`, `core.middleware`, `build_model`, `Attribution`.
- `pyyaml` — the drill format is YAML with multi-document `setup` blocks.
- `httpx` — the kubiq HTTP API.
- `git`, `kubectl`, `docker compose` on PATH; `gh` optional.
- Dev: `pytest`, `pytest-cov`, `ruff` (line length 90; `E,F,I,UP,B,SIM,RUF`), `pyright` (standard mode, 3.13), `types-PyYAML`.
