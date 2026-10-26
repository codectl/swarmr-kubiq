# swarmr-kubiq

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a
Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq) — the drill
files and the exam manifest — and proves every question on a live kubiq
session before the files are handed to you for review.

Nothing is filed on trust. An author drafts each question against the official
CNCF curriculum and the existing catalogue so it is neither out of scope nor a
rewording of one already there; the harness runs every question through a real
candidate shell in one session; a critic tries to disprove each proof from its
record. Verdicts are kept under `~/.cache/swarmr-kubiq/`, so a rerun proves only
what changed. A question the harness cannot prove is reported as `unproven`,
never quietly dropped or quietly kept.

## Install

Core and this team must land in the **same environment**.

```
uv tool install "swarmr[kubiq]" --with-executables-from swarmr-kubiq
```

That exposes `teams` and `teams-mcp` on your PATH; this team ships no command
of its own. From a checkout:
`uv tool install ../swarmr --with ../swarmr-kubiq`.

Run it from inside the kubiq checkout:

```
cd kubiq
teams --target exam_author --exam ckad-mock-7            # profile only, no model
teams exam_author --exam ckad-mock-7 --version 1.35 "create a new CKAD mock exam"
teams exam_author --exam cka-mock-6 --certification cka "21 questions, two recent-feature questions"
```

Over MCP the tool is
`start_exam_author(request, exam, kubiq?, certification?, server?, version?, questions?)`.

## Prerequisites

- **A running kubiq** serving the checkout, built from a checkout with hot
  reload: `docker compose up -d --build` once. Its compose file bind-mounts
  `drills/` and `exams/`, so the files the team stages are visible to the
  server at once; the tools ask kubiq to check they load (`POST /api/check`)
  and reload the catalogue before a proof (`POST /api/reload`). kubiq refuses
  a reload while any session is open — it would change the exam someone is
  sitting — and the team then stops touching the cluster and reports it. The
  team never rebuilds or stops kubiq.
- **`KUBECONFIG`** pointing at the cluster kubiq provisions sessions on. The
  harness types into the candidate shell with
  `kubectl exec -n kubiq-sessions cand-<session> -- bash -lc '<cmd>'`, and reads
  the cluster's version from it.
- **Model credentials** come from `swarmr` (`KIMI_*`).
- **lodestar** (optional): the author and the critic read the indexed Kubernetes
  documentation through the lodestar MCP — launched over stdio from
  `LODESTAR_COMMAND` or the `lodestar` entry in `~/.omp/agent/mcp.json`, or
  reached over HTTP at `LODESTAR_URL`. Unreachable means empty search results
  and no recent-feature questions, not a failure.

## What the team holds a question to

- **The official curriculum.** The CNCF domain list for the chosen
  certification — percentages and competencies verbatim from
  [cncf/curriculum](https://github.com/cncf/curriculum) — is in every prompt.
  The commander assigns each slot one competency through `plan_exam`, which
  allocates the weights from the domain shares so they sum to exactly 100 and
  refuses a slot whose competency is not quoted verbatim; the critic refutes a
  question no competency covers: a CKAD exam never administers the control
  plane, a CKA exam is not application packaging.
- **The test cluster.** `--version` names the Kubernetes minor to author
  against. If it is above the cluster's version the run stops before the first
  model call: a 1.37 question cannot be proven on a 1.35 cluster.
- **Competencies the cluster cannot host** (kubeadm, control-plane manifests,
  kubelet/apiserver flags, audit policy, CIS, AppArmor) are planned as
  file-graded questions: the drill places the file in the candidate shell, the
  task asks for the exact change, exec checks grade the file. The report says
  which questions prove an effect and which prove a file.
- **Uniqueness.** Every drill is scored against the whole catalogue and the
  other questions of the exam before staging; a neighbour at 0.62 or above is
  refused, and the author redesigns around a different mechanic in the same
  domain.

## What the harness proves

For every question, in ONE kubiq session for the whole exam (a fixed question
is re-proven by its author in a session of its own), in this order:

1. **Negative control** — `check` must *fail* before anything is done. A check
   that passes on an untouched namespace proves nothing. The result is kept
   in the verdict for the critic.
2. **Settle** — the setup's Pods must be running, finished or deliberately
   stuck before the solution runs, so a grade never depends on a race with
   image pulls.
3. **Solve** — the drill's `solution` runs in the candidate shell, exactly as
   written. The rule is *fix the solution, never the task*: the task text is
   frozen at first staging and `stage_drill` refuses a changed task.
4. **Check** — polled every 3 s until every check passes or
   `KUBIQ_SETTLE_SECONDS` (default 90) elapses, with the grader's per-check
   detail kept as evidence.
5. **End** — the session is finished and graded.
6. **Teardown proof** — after the session is deleted, no namespace, CRD,
   PersistentVolume, ClusterRole or ClusterRoleBinding from the drill remains.

Only one kubiq session can exist at a time. The harness creates its own and
never deletes one it did not create; a foreign session is a refusal, not a
cleanup.

## Finishing

The team never runs git. When every question has a verdict, `finalize_exam`
writes the exam — confirmed questions only — into the kubiq checkout under
`exams/<exam>.yaml` and `drills/<exam>/` (a draft when fewer than planned
survived), and the report lists those paths with the `git checkout -b
exam/<exam>`, `git add`, `git commit`, `git push` and `gh pr create` lines to
run after you have reviewed the files.

The report lists each question with its weight, verdict (`confirmed`,
`refuted`, `unproven`) and the number of author briefings it took, the
questions covering recent-version material, and any caveats.

## Arguments

Per call, as `--flags` on the CLI or arguments of the MCP tool. Nothing about
the target comes from the environment: a process serving several callers cannot
have one checkout.

`exam` is the identifier to author, e.g. `ckad-mock-7` — required, must not
exist yet. `kubiq` is the checkout to write into, the root holding `drills/` and
`exams/`; defaults to the current directory. `certification` is `CKAD`
(default), `CKA` or `CKS`. `version` is the Kubernetes minor to author against,
e.g. `1.35`; must not exceed the test cluster's. `server` is the running kubiq,
default `http://localhost:8080`. `questions` defaults to 21 (CKAD/CKA) or 16
(CKS).

Tuning: `KUBIQ_SOLVE_BUDGET` (seconds a solution plus convergence may take,
default 120), `KUBIQ_SETTLE_SECONDS` (cap on grader convergence after a
solution, default 90), `XDG_CACHE_HOME` (where proven verdicts are kept,
default `~/.cache`), `LODESTAR_COMMAND` / `LODESTAR_URL` (how to reach
lodestar).

## References

- [Architecture overview](./CLAUDE.md)
