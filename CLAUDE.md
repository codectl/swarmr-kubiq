# CLAUDE.md — swarmr-kubiq

## Overview

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq) — drill files under `drills/<exam>/` and the manifest `exams/<exam>.yaml` — and proves every question on a live kubiq session before handing the files to the operator. A commander plans slots from the official CNCF curriculum with the deterministic `plan_exam` tool and delegates to an author (drafts questions against the competency and the existing catalogue, proves its own fixes) and a critic (tries to disprove the proof record and the question's scope, with no cluster access); only confirmed questions are written.

Mutating by design, but only in two places: the kubiq checkout (files under the exam's own paths only; git is never run — the operator reviews and commits) and the running kubiq (sessions the harness itself created, and a catalogue reload through `POST /api/reload`, which kubiq refuses while any session exists). kubiq is never rebuilt or stopped by this team: its compose file bind-mounts `drills/` and `exams/`, so a staged file is visible to the server as soon as it is written and `POST /api/check` reports whether it loads. A session the harness did not create is never deleted. Proofs are persisted under `~/.cache/swarmr-kubiq/` keyed by the drill's content, so a rerun re-proves only what changed. Nothing about the catalogue is hardcoded — `catalogue.py` profiles the checkout at build time and the profile is injected into every prompt.

- **Distribution:** `swarmr-kubiq` (`src/swarmr_kubiq`, hatchling)
- **Python:** >=3.13; `kubectl` on PATH; a running kubiq built from a checkout with `/api/reload` whose compose file bind-mounts `drills/` and `exams/`; `lodestar` optional
- **Deps:** `swarmr>=1.3,<2`, `pyyaml`, `httpx`
- **Entry point:** `swarmr.teams` group → `exam_author = "swarmr_kubiq:TEAM"`
- **Script:** none

**The roster.** `commander` plans slots with `plan_exam` (domain, competency, namespace, effect- or file-graded, recent-feature version; the tool allocates weights from the curriculum shares), proves them with `prove_all` (one session for the whole exam, skipping questions already proven) and `prove_questions` (a batch re-proof), runs the fix loop, finalises the files and files the report; it holds no authoring tools. `author` drafts drills, stages them and proves every draft itself with `prove_questions` (at most three proofs per briefing), fixing in its own context; the commander's `prove_all` is the safety net that skips what is already proven. `critic` adjudicates batches of 5–7 proven questions from the proof record alone (`grader_details`: negative control, grades, solve time, leftovers), the task text against the checks, docs claims and curriculum fit; its word is the verdict. A proof is a harness run with a deterministic result, so no model sits between "prove" and "verdict".

## Entry Points

### The team (`__init__.py` → `TEAM`)

Reached through `swarmr`'s surfaces, not its own:

```
teams --target exam_author --exam ckad-mock-7
teams exam_author --exam ckad-mock-7 --version 1.35 "create a new CKAD mock exam"
```

Over MCP the tool is `start_exam_author(request, exam, kubiq?, certification?, server?, version?, questions?)`.

Importing this module must stay cheap: it is what `teams --list` and the MCP server read to publish the team. `build`, `profile` and `render_report` are declared with `Lazy`; only `digest`, `is_error` and `target.PARAMS` are imported directly.

**Per-call arguments** (`target.PARAMS`): `exam` (required; must not exist yet), `kubiq` (checkout root holding `drills/` and `exams/`; default cwd), `certification` (CKAD default, CKA, CKS), `server` (running kubiq, default `http://localhost:8080`), `version` (Kubernetes minor to author against; `catalogue.check_version` refuses it above the cluster's `kubectl version`), `questions` (21, or 19 for CKS — the smallest count whose weights from {1,2,4,8} can match the CKS domain shares exactly; `plan_exam` refuses a count that cannot). **Nothing about the target comes from the environment** except the cwd default.

**Environment (tuning only):** `KUBECONFIG` (the cluster kubiq provisions sessions on; `kubectl exec` into `cand-<session>` uses it), `KUBIQ_SOLVE_BUDGET` (seconds a solution plus convergence may take before the question is refused as too slow, default 120), `KUBIQ_SETTLE_SECONDS` (cap on grader convergence polling after a solution, default 90), `XDG_CACHE_HOME` (where persisted proofs live; default `~/.cache`), `LODESTAR_COMMAND` / `LODESTAR_URL` (lodestar MCP over stdio or HTTP; default is the `lodestar` entry of `~/.omp/agent/mcp.json`; unreachable → empty results with a `tool error:` note). Model credentials come from `swarmr` (`KIMI_*`).

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
`kubiq_api.py` — HTTP client for a running kubiq: list exams, create session, wait ready, exec in the candidate shell (`kubectl exec -n kubiq-sessions cand-<id>`), check a question, finish, delete, `reload()` (`POST /api/reload`: swap the catalogue from disk) and `check()` (`POST /api/check`: the loader's error text, or `""`). Refuses to touch a session it did not create. `FatalError` is sticky: kubiq unreachable, no `/api/reload` (an old build), or a reload refused with 409 because a foreign session is open — the run must stop touching the cluster. A 422 from the loader is a `KubiqError` with its text. `harness.py` — `stage_exam` writes exam + drills into the checkout; `prove_exam(client, target, exam, drills, indexes=None)`: one session for the whole exam, and for each requested question negative control → settle gate (`settle_setup` waits up to `SETUP_SETTLE_MAX` for the setup's Pods to run, finish or get deliberately stuck) → solve → poll `check` until pass or `KUBIQ_SETTLE_SECONDS` → finish → delete → teardown proof (cluster-scoped kinds before/after); returns a `Verdict` per question with the grader's detail, `results_before` and `setup_settle_seconds`.

**Proof persistence**\
`proofs.py` — `proof_key(drill, namespace)` (sha256 of the dumped drill and its namespace), `cache_path(target)` (`$XDG_CACHE_HOME/swarmr-kubiq/<checkout hash>/<exam>.json`), `load_proofs` / `save_proof`. A verdict is reused only while the drill and namespace are byte-identical to what was proven.

**Uniqueness and docs**\
`similarity.py` — token-overlap similarity of task texts and check paths against the catalogue; nearest neighbours with scores. `docs.py` — lodestar MCP client (`search_docs`, `get_page`) over stdio or streamable HTTP; failures degrade to empty results.

**Prompts**\
`prompts.py` — `commander`, `author`, `critic`, the shared `CONTRACT`, `SWEEP_REQUEST`. No catalogue fact appears here; everything target-specific arrives as `facts`.

**Tools**\
`tools.py` — the `@tool`s per role: `AUTHOR_TOOLS`, `CRITIC_TOOLS`, `COMMANDER_TOOLS`. `plan_exam` validates N slots against `CURRICULUM` (verbatim competencies, namespace shape and uniqueness, every domain covered) and allocates weights per domain with `allocate_weights` (values from {1,2,4,8} summing exactly to the share). `stage_drill` is the mechanical gate: strict validation, drill-id ownership, frozen task text, similarity against the catalogue and the staged siblings, a setup dry run, then `client.check()` on the written files, with rollback on any failure. Module state holds the plan (`_plan`), the exam under construction (`_staged`), task text at first staging, verdicts, the digest of what the server last loaded (reload only when staging changed) and a sticky `_fatal` that every cluster tool re-raises through `_cluster()`. `resume_staging` rebuilds the state from the checkout and the persisted proofs.

**Result shaping**\
`output.py` — `emit` (byte cap), `guard` (exception containment; a failure is text starting `tool error:`). `digest.py` — `digest_result`, `is_tool_error`.

**Report and PR**\
`report_tool.py` — `file_exam_report` tool and `render_report_args`: header with counts and confirmed weight, one line per question (`NN. <title> — <weight> pts — <verdict> (rounds)`), new-feature coverage, the files written with the `git checkout -b`/`add`/`commit`/`push`/`gh pr create` lines for the operator, caveats; renders with any keys missing. `pr.py` — `branch_name(exam)`, `commit_message(exam, n)`: the naming convention only; nothing here runs git.

## Roles and Tool Sets

|Role|Tools|Permissions|
|---|---|---|
|`commander`|`plan_exam`, `prove_all`, `prove_questions`, `grader_details`, `finalize_exam`, `file_exam_report`|`_NO_WRITES`|
|`author`|`catalogue_search`, `read_drill`, `search_docs`, `read_doc`, `stage_drill`, `prove_questions`, `grader_details`|exam paths only|
|`critic`|`grader_details`, `read_drill`, `catalogue_search`, `search_docs`, `read_doc`|`_NO_WRITES`|

**Only `prove_all` and `prove_questions` create sessions.** The critic never drafts or rewrites a drill and never touches the cluster; the author reaches the cluster only through `prove_questions`, proving every draft it stages. A verdict is the critic's word: `confirmed`, `refuted` or `unproven`.

**The commander holds no catalogue or cluster tools by design:** its context stays clean, and it cannot fabricate an observation it never received. `finalize_exam` rewrites the exam with the confirmed questions only; `file_exam_report` files the report. Neither runs git.

## Flow

```
commander plan_exam → authors stage → prove_all (one session, skips proven) → per failure: author stages+proves (≤2) → critic batches (no cluster) → finalize_exam → file_exam_report
```

A failed proof goes back to the author with the grader's detail; the author stages the fix and proves it with `prove_questions` (at most three proofs per briefing, then one REPLACE briefing, then the slot is dropped). The task text is frozen at first staging — `stage_drill` refuses a changed task — so only solution, checks and setup may change. `rounds` in the report counts author briefings. A `tool error: FATAL` stops all cluster work and the report files what exists. Competencies the cluster cannot host are planned as file-graded questions (`files` + exec checks in the candidate shell).

## Testing

No model calls, no cluster, no `docker`. `testpaths = ["src"]`, tests beside the code they cover, excluded from the wheel. Fixtures are temp kubiq-like trees; `test_drill.py` also parses every shipped drill when the kubiq checkout sits beside this repo; `harness.py` and `kubiq_api.py` are driven with fake clients and a fake `_run`; `test_proofs.py` points `XDG_CACHE_HOME` at a temp dir.

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
- `kubectl` on PATH; `lodestar` optional.
- Dev: `pytest`, `pytest-cov`, `ruff` (line length 90; `E,F,I,UP,B,SIM,RUF`), `pyright` (standard mode, 3.13), `types-PyYAML`.
