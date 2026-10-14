# swarmr-kubiq

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a
Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq) — the drill
files and the exam manifest — and proves every question on a live kubiq
session before the files are handed to you for review.

Nothing is filed on trust. An author drafts each question against the official
CNCF curriculum and the existing catalogue so it is neither out of scope nor a
rewording of one already there; a tester runs it through a real candidate
shell; a critic tries to disprove the tester's verdict. A question the harness
cannot prove is reported as `unproven`, never quietly dropped or quietly kept.

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

- **A running kubiq** serving the checkout: `make docker-up` in that checkout.
  kubiq loads drills and exams from disk at startup, so the tester writes the
  staged files into the checkout and restarts the server
  (`docker compose up -d --build --force-recreate`, about 15 s when only YAML
  changed) before a proof round. It refuses to restart while any session is
  open — a restart would end an exam someone is sitting.
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
  The commander assigns each slot one competency, weights track the domain
  shares, and the critic refutes a question no competency covers: a CKAD exam
  never administers the control plane, a CKA exam is not application packaging.
- **The test cluster.** `--version` names the Kubernetes minor to author
  against. If it is above the cluster's version the run stops before the first
  model call: a 1.37 question cannot be proven on a 1.35 cluster.
- **Competencies the cluster cannot host** (kubeadm, control-plane manifests,
  kubelet/apiserver flags, audit policy, CIS, AppArmor) are planned as
  file-graded questions: the drill places the file in the candidate shell, the
  task asks for the exact change, exec checks grade the file. The report says
  which questions prove an effect and which prove a file.
- **Uniqueness.** Every drill is scored against the whole catalogue before
  staging; a neighbour at 0.62 or above sends the author back to a different
  mechanic in the same domain.

## What the harness proves

For every question, on a fresh kubiq session, in this order:

1. **Negative control** — `check` must *fail* before anything is done. A check
   that passes on an untouched namespace proves nothing.
2. **Solve** — the drill's `solution` runs in the candidate shell, exactly as
   written. The rule is *fix the solution, never the task*: the task text is
   frozen at first staging and the critic refutes a question whose task changed.
3. **Check** — polled every 3 s until every check passes or
   `KUBIQ_SETTLE_SECONDS` (default 90) elapses, with the grader's per-check
   detail kept as evidence.
4. **End** — the session is finished and graded.
5. **Teardown proof** — after the session is deleted, no namespace, CRD,
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
`refuted`, `unproven`) and the number of author/tester rounds it took, the
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

Tuning: `KUBIQ_SETTLE_SECONDS` (cap on grader convergence after a solution,
default 90), `LODESTAR_COMMAND` / `LODESTAR_URL` (how to reach lodestar).

## References

- [Architecture overview](./CLAUDE.md)
