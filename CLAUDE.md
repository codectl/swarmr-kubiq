# CLAUDE.md — swarmr-kubiq

## Overview

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq) — drill files under `drills/<exam>/` and the manifest `exams/<exam>.yaml` — and proves every question on a live kubiq session before handing the files to the operator. A commander plans slots from the official CNCF curriculum and delegates to an author (drafts questions against the competency and the existing catalogue), a tester (runs each drill through a real candidate shell) and a critic (tries to disprove the tester's verdict and the question's scope); only confirmed questions are written.

Mutating by design, but only in two places: the kubiq checkout (files under the exam's own paths only; git is never run — the operator reviews and commits) and the running kubiq (sessions the harness itself created, and a restart only when no session exists). A session the harness did not create is never deleted. Nothing about the catalogue is hardcoded — `catalogue.py` profiles the checkout at build time and the profile is injected into every prompt.

- **Distribution:** `swarmr-kubiq` (`src/swarmr_kubiq`, hatchling)
- **Python:** >=3.13; `kubectl` and `docker compose` on PATH; `lodestar` optional
- **Deps:** `swarmr>=1.3,<2`, `pyyaml`, `httpx`
- **Entry point:** `swarmr.teams` group → `exam_author = "swarmr_kubiq:TEAM"`
- **Script:** none

**The roster.** `commander` plans slots (domain, competency, weight, namespace, effect- or file-graded, recent-feature version), runs the loop, finalises the files and files the report; it holds no catalogue or cluster tools. `author` drafts drills and stages them. `tester` proves each drill on a live session. `critic` re-runs the negative control itself, checks task immutability, similarity, images, docs claims and curriculum fit; its word is the verdict.

## Entry Points

### The team (`__init__.py` → `TEAM`)

Reached through `swarmr`'s surfaces, not its own:

```
teams --target exam_author --exam ckad-mock-7
teams exam_author --exam ckad-mock-7 --version 1.35 "create a new CKAD mock exam"
```

Over MCP the tool is `start_exam_author(request, exam, kubiq?, certification?, server?, version?, questions?)`.

Importing this module must stay cheap: it is what `teams --list` and the MCP server read to publish the team. `build`, `profile` and `render_report` are declared with `Lazy`; only `digest`, `is_error` and `target.PARAMS` are imported directly.

**Per-call arguments** (`target.PARAMS`): `exam` (required; must not exist yet), `kubiq` (checkout root holding `drills/` and `exams/`; default cwd), `certification` (CKAD default, CKA, CKS), `server` (running kubiq, default `http://localhost:8080`), `version` (Kubernetes minor to author against; `catalogue.check_version` refuses it above the cluster's `kubectl version`), `questions` (21, or 16 for CKS). **Nothing about the target comes from the environment** except the cwd default.

**Environment (tuning only):** `KUBECONFIG` (the cluster kubiq provisions sessions on; `kubectl exec` into `cand-<session>` uses it), `KUBIQ_SETTLE_SECONDS` (cap on grader convergence polling after a solution, default 90), `LODESTAR_COMMAND` / `LODESTAR_URL` (lodestar MCP over stdio or HTTP; default is the `lodestar` entry of `~/.omp/agent/mcp.json`; unreachable → empty results with a `tool error:` note). Model credentials come from `swarmr` (`KIMI_*`).

## Architecture

**Team declaration**\
`__init__.py` — the `Team` object: name `exam_author`, summary, routing description, vocabulary (`orchestrator="commander"`, `audit_agents=("critic",)`, `report_tool="file_exam_report"`, `digest`, `is_error`), roster, `params`, and the `Lazy` targets.

**Assembly**\
`agent.py` — `build(run)` and `profile_target(run)`. Both begin with `target.activate(run.params)`. Stock Deep Agents: one `create_deep_agent` commander plus three `SubAgent` specialists reached through the built-in `task` tool. Holds the filesystem permission rule sets.

**Target**\
`target.py` — `PARAMS`, `Target(kubiq, exam, certification, server, questions, version)` with `.exam_file`/`.drill_dir`, `activate(params)` (validates the checkout root, the exam id shape, that the exam does not exist yet; sets the ContextVar), `current()` (`TargetError(TeamError)` outside a run).

**Drill schema**\
`drill.py` — `Drill`, `Check`, `Exam`, `Question` dataclasses mirroring kubiq's `src/drill.rs`; `parse_drill` / `validate_drill` / `dump_drill` / `load_drill` and the exam counterparts; `DrillError`. Validation is the only place the format's rules live (ids, order, weights, check shapes, the `/opt/student-node/start.d/<id>.sh` hook convention).

**Catalogue**\
`catalogue.py` — `profile_catalogue()` reads every drill and exam in the checkout: ids, titles, topics, task texts, namespaces, broken files; `render_facts()` and `banner()` turn it into prompt text; `CURRICULUM` holds the per-certification domains, shares and competencies verbatim from cncf/curriculum (CKAD v1.37, CKA v1.35, CKS v1.34); `cluster_version()` reads `kubectl version`, `check_version()` refuses a `--version` above it.

**Cluster side**\
`kubiq_api.py` — HTTP client for a running kubiq: list exams, create session, wait ready, exec in the candidate shell (`kubectl exec -n kubiq-sessions cand-<id>`), check a question, finish, delete. Refuses to touch a session it did not create. `restart_server` refuses while any session exists. `harness.py` — `stage_exam` writes exam + drills into the checkout; `prove_question` / `prove_exam`: negative control → solve → poll `check` until pass or `KUBIQ_SETTLE_SECONDS` → finish → delete → teardown proof (cluster-scoped kinds before/after); returns a `Verdict` with the grader's detail.

**Uniqueness and docs**\
`similarity.py` — token-overlap similarity of task texts and check paths against the catalogue; nearest neighbours with scores. `docs.py` — lodestar MCP client (`search_docs`, `get_page`) over stdio or streamable HTTP; failures degrade to empty results.

**Prompts**\
`prompts.py` — `commander`, `author`, `tester`, `critic`, the shared `CONTRACT`, `SWEEP_REQUEST`. No catalogue fact appears here; everything target-specific arrives as `facts`.

**Tools**\
`tools.py` — the `@tool`s per role: `AUTHOR_TOOLS`, `TESTER_TOOLS`, `CRITIC_TOOLS`, `COMMANDER_TOOLS` (`finalize_exam` + the report tool). Module state holds the exam under construction (`_staged`), task text at first staging, last verdicts, and the digest of what the server last loaded (restart only when staging changed).

**Result shaping**\
`output.py` — `emit` (byte cap), `guard` (exception containment; a failure is text starting `tool error:`). `digest.py` — `digest_result`, `is_tool_error`.

**Report and PR**\
`report_tool.py` — `file_exam_report` tool and `render_report_args`: header with counts and confirmed weight, one line per question (`NN. <title> — <weight> pts — <verdict> (rounds)`), new-feature coverage, the files written with the `git checkout -b`/`add`/`commit`/`push`/`gh pr create` lines for the operator, caveats; renders with any keys missing. `pr.py` — `branch_name(exam)`, `commit_message(exam, n)`: the naming convention only; nothing here runs git.

## Roles and Tool Sets

|Role|Tools|Permissions|
|---|---|---|
|`commander`|`finalize_exam`, `file_exam_report`|`_NO_WRITES`|
|`author`|`catalogue_search`, `read_drill`, `search_docs`, `read_doc`, `similar_drills`, `validate_drill_yaml`, `stage_drill`|exam paths only|
|`tester`|`prove_question`, `shell`, `grader_details`|exam paths only|
|`critic`|`negative_control`, `diff_task`, `read_drill`, `catalogue_search`, `similar_drills`, `validate_drill_yaml`, `search_docs`, `read_doc`|`_NO_WRITES`|

**Only the tester and the critic's `negative_control` create sessions.** The critic never drafts or rewrites a drill; the author never touches the cluster. A verdict is the critic's word: `confirmed`, `refuted` or `unproven`.

**The commander holds no catalogue or cluster tools by design:** its context stays clean, and it cannot fabricate an observation it never received. `finalize_exam` rewrites the exam with the confirmed questions only; `file_exam_report` files the report. Neither runs git.

## Flow

```
commander plans N slots → per slot: author → tester ⇄ author (verbatim failure, ≤3 rounds) → critic (one retry) → finalize_exam → file_exam_report
```

A failed proof goes back to the author with the grader's detail; the task text is frozen at first staging (`diff_task`), only solution, checks and setup may change. `rounds` in the report counts those trips. Competencies the cluster cannot host are planned as file-graded questions (`files` + exec checks in the candidate shell).

## Testing

No model calls, no cluster, no `docker`. `testpaths = ["src"]`, tests beside the code they cover, excluded from the wheel. Fixtures are temp kubiq-like trees; `test_drill.py` also parses every shipped drill when the kubiq checkout sits beside this repo; `harness.py` and `kubiq_api.restart_server` are driven with fake clients and a fake `_run`.

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
- `kubectl`, `docker compose` on PATH; `lodestar` optional.
- Dev: `pytest`, `pytest-cov`, `ruff` (line length 90; `E,F,I,UP,B,SIM,RUF`), `pyright` (standard mode, 3.13), `types-PyYAML`.
