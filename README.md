# swarmr-kubiq

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a
Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq) — the drill
files and the exam manifest — and proves every question on a live kubiq
session before it opens a pull request.

Nothing is filed on trust. An author drafts each question against the existing
catalogue so it is not a rewording of one already there; a tester runs it
through a real candidate shell; a critic tries to disprove the tester's verdict.
A question the harness cannot prove is reported as `unproven`, never quietly
dropped or quietly kept.

## Install

Core and this team must land in the **same environment**.

```
uv tool install "swarmr[kubiq]" --with-executables-from swarmr-kubiq
```

That exposes `teams` and `teams-mcp` on your PATH; this team ships no command
of its own. From a checkout:
`uv tool install ../swarmr --with ../swarmr-kubiq`.

```
teams --target exam_author --kubiq ../kubiq --exam ckad-mock-7
teams exam_author --kubiq ../kubiq --exam ckad-mock-7 "CKAD, 21 questions, include 1.37 features"
```

Over MCP the tool is `start_exam_author(request, kubiq, exam, certification?, server?, questions?)`.

## Prerequisites

- **A running kubiq** serving the checkout you point `--kubiq` at: `make docker-up`
  in that checkout. kubiq loads drills and exams from disk at startup, so the
  tester writes the candidate files into the checkout and restarts the server
  (`docker compose up -d --build --force-recreate`, about 15 s when only YAML
  changed) before each proof round.
- **`KUBECONFIG`** pointing at the cluster kubiq provisions sessions on. The
  harness types into the candidate shell with
  `kubectl exec -n kubiq-sessions cand-<session> -- bash -lc '<cmd>'`.
- **Model credentials** come from `swarmr` (`KIMI_*`).
- **lodestar** (optional): when the lodestar MCP is reachable the author reads
  upstream "what's new" pages to cover recent-version material; without it a
  static list of recent features is used.
- **`gh`** (optional): logged in, the run opens the PR itself; otherwise the
  report prints the push command.

## What the harness proves

For every question, on a fresh kubiq session, in this order:

1. **Negative control** — `check` must *fail* before anything is done. A check
   that passes on an empty cluster proves nothing.
2. **Solve** — the drill's `solution` runs in the candidate shell, exactly as
   written. The rule is *fix the solution, never the task*: a solution that does
   not satisfy its own checks is the author's bug, not the candidate's.
3. **Check** — every check must pass, with the grader's per-check detail kept
   as evidence.
4. **End** — the session is finished and graded; the score must agree with the
   checks.
5. **Teardown proof** — after the session is deleted, no namespace, CRD,
   PersistentVolume, ClusterRole or ClusterRoleBinding from the drill remains.

Only one kubiq session can exist at a time. The harness creates its own and
never deletes one it did not create; a foreign session is a refusal, not a
cleanup.

## The PR flow

When every question has a verdict, the run commits the exam on
`exam/<exam>` in the kubiq checkout — **only the files it wrote**. A checkout
with uncommitted changes to other files is refused rather than swept into the
exam commit. With `gh` logged in the PR is opened against `main` and its URL is
in the report; otherwise the report carries
`git push -u origin exam/<exam>` and the `gh pr create` line to run yourself.

The report lists each question with its weight, verdict (`confirmed`,
`refuted`, `unproven`) and the number of author/tester rounds it took, the
questions covering recent-version material, and any caveats.

## Arguments

Per call, as `--flags` on the CLI or arguments of the MCP tool. Nothing about
the target comes from the environment: a process serving several callers cannot
have one checkout.

`kubiq` is the checkout to write into — required, the root holding `drills/`
and `exams/`. `exam` is the identifier to author, e.g. `ckad-mock-7` — required,
must not exist yet. `certification` is `CKAD` (default), `CKA` or `CKS`.
`server` is the running kubiq, default `http://localhost:8080`. `questions`
defaults to 21 (CKAD/CKA) or 16 (CKS).

Tuning: `KUBIQ_SETTLE_SECONDS` (how long to wait for kubiq after a restart
before the first session is created), `LODESTAR_URL` (the lodestar MCP
endpoint; unset means the static feature list).

## References

- [Architecture overview](./CLAUDE.md)
