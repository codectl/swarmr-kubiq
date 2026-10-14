# swarmr-kubiq — build plan

Agent team that authors Kubernetes mock exams for kubiq and proves every
question on a live cluster before opening a PR. Team name `exam_author`,
MCP tool `start_exam_author`. Mirrors the layout of swarmr-kube / swarmr-blame.

Legend: [ ] todo · [~] in progress · [x] done · [!] blocked

## 0. Scaffold
- [x] TODO.md (this file)
- [x] pyproject.toml (hatchling, `swarmr.teams` entry point, dev extras, ruff/pyright/pytest config)
- [x] CLAUDE.md, README.md, CHANGELOG.md, .github/workflows/{ci,publish}.yml

## 1. Contracts (shared by every module)
- [x] `target.py` — PARAMS: `kubiq` (required; checkout root of codectl/kubiq), `exam` (required; e.g. `ckad-mock-7`), `certification` (optional; CKAD default), `server` (optional; running kubiq URL, default http://localhost:8080). ContextVar `Target`, `activate()`, `current()`.
- [x] `drill.py` — the kubiq drill YAML schema as dataclasses + `parse_drill()` / `dump_drill()` / `validate()` (ids, order, weights, checks shape). Mirrors kubiq `src/drill.rs`.
- [x] `catalogue.py` — reads `drills/**/*.yaml` + `exams/*.yaml` from the kubiq checkout: existing ids, titles, topics, task texts, namespaces; renders `facts` for prompts. (= discovery.py of the other teams)

## 2. Tools
- [x] `kubiq_api.py` — `KubiqClient` (exams, sessions, create/wait_ready/check/finish/delete, `shell_exec` via `kubectl exec` into `cand-<id>`), `restart_server(checkout, client, exam_id, questions)` polling `/api/exams`, `KubiqError`; `_run`/`_sleep` seams.
- [x] `harness.py` — `stage_exam` (draft when short, else validated; `qNN-<id>.yaml`, stale files removed), `prove_question` (refuse leftover session → create → wait_ready → negative control → solution in shell (comments stripped, heredocs intact) → settle → check → finish → teardown diff → delete), `prove_exam` (one session for all questions), `Verdict.to_json()` kind=verdict.
- [x] `similarity.py` — fingerprint (kinds, verbs, normalised assert paths, task tokens minus names) + weighted Jaccard; `nearest()` with reasons, `THRESHOLD` 0.62.
- [x] `docs.py` — lodestar MCP client (`search_docs`, `get_page`, `whats_new`) over stdio (omp mcp.json / `LODESTAR_COMMAND`) or streamable HTTP (`LODESTAR_URL`); every failure → `[]`/`""` + `.note`.
- [x] `tools.py` — LangChain `@tool`s per role + role tool sets: AUTHOR_TOOLS, TESTER_TOOLS, CRITIC_TOOLS, COMMANDER_TOOLS (report only); staging state (`_staged`, task baseline, verdicts, server digest), `write_exam_file`, `reset_staging`.
- [x] `output.py` — `emit` (byte cap), `guard` (ValueError/DrillError, TargetError, KubiqError, httpx errors, Exception → `tool error:`).
- [x] `digest.py` — `digest_result` (verdict, similarity, session, docs + shape fallback), `is_tool_error`; test_digest.py.

## 3. Agents
- [x] `prompts.py` — commander, author, tester, critic, CONTRACT, SWEEP_REQUEST. Rules baked in: uniqueness vs catalogue; "fix the solution, never the task"; negative control; CKAD curriculum weights; no vendor images (allowed set from drill.py).
- [x] `agent.py` — `build(run)`, `profile_target(run)`; roster commander/author/tester/critic; filesystem permissions.
- [x] `report_tool.py` — `file_exam_report` tool + `render_report_args`: per-question verdict table, files written, PR branch/commit suggestion.
- [x] `pr.py` — naming convention only (branch/commit message). The team never runs git: `finalize_exam` writes files, the report prints the commands for the operator.
- [x] `__init__.py` — `TEAM` declaration (Lazy build/profile/render_report).

## 4. Tests (pytest, beside the code, excluded from wheel)
- [x] test_contract.py — vocabulary, roster, lazy fields, tool sets per role (+ test_tools.py — staging state machine against a tmp checkout)
- [x] test_drill.py — parse/dump/validate round trip on real kubiq drills
- [x] test_catalogue.py — facts rendering from a temp kubiq-like tree
- [x] test_similarity.py — renamed run+expose pair ≥ THRESHOLD, headless Service < THRESHOLD, symmetry, nearest ranking with reason
- [x] test_docs.py — mocked httpx transport (JSON + SSE), isError/transport failures degrade, whats_new dedupe
- [x] test_harness.py — fake kubiq + fake kubectl: leftover refusal, call order, negative control, heredoc intact, verdict JSON, leftovers, prove_exam, stage_exam round trip/draft/weights
- [x] test_report.py — render_report_args (+ test_pr.py — naming)
- [x] test_target.py — params validation

## 5. Verification
- [x] Harness smoke on live kubiq (ckad-mock-6 q1): proven, negative control failed, converged in 15.7 s, teardown clean
- [x] `restart_server` refuses while any session is open (a recreate would kill a person's exam)
- [x] `uv venv && uv pip install -e ../swarmr -e ".[dev]"`; pytest (106), ruff, pyright clean
- [x] `teams --list` shows exam_author; `teams --target exam_author --kubiq ../kubiq --exam ckad-mock-7` profiles without a model key
- [ ] End-to-end smoke against the local kubiq (NEEDS: session slot free + model key; restarts kubiq and writes drills/ckad-mock-7 into the checkout): generate 1 question, harness proves it (needs model key + running kubiq)

## Decisions
- Test strategy: kubiq reads drills from disk at startup, so the harness writes the candidate files into the kubiq checkout and restarts the server (`docker compose up -d --build --force-recreate`). A dedicated "load exam from request" endpoint would avoid restarts but is out of scope for this repo.
- The team never commits: files are written into the checkout, the operator reviews and opens the PR. kubiq itself never depends on this package.
