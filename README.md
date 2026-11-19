# swarmr-kubiq

Exam authoring team for [swarmr](https://github.com/codectl/swarmr). Writes a
Kubernetes mock exam for [kubiq](https://github.com/codectl/kubiq), the drill
files and the exam manifest, and proves every question on a live kubiq session
before the files are handed to you for review.

Mutating by design, in two places only: the kubiq checkout, under the exam's own
paths, and the running kubiq, through sessions the team itself opens and a
catalogue reload. Git is never run: you review and commit. Nothing about the
catalogue is hardcoded: the team profiles the checkout at startup and injects
what it found into every prompt.

## Install

Core and this team must land in the **same environment**.

```
uv tool install swarmr --with swarmr-kubiq
```

That exposes `teams` and `teams-mcp` on your PATH; this team ships no command of
its own. From a checkout: `uv tool install ../swarmr --with ../swarmr-kubiq`.

```
teams --target exam_author --exam ckad-mock-7
teams exam_author --exam ckad-mock-7 --version 1.35 "create a new CKAD mock exam"
```

Over MCP the tool is
`start_exam_author(request, exam, kubiq?, certification?, server?, version?, questions?, difficulty?, topics?)`.

## The proof

The team does not trust a question it has not run. Every drill is staged into
the checkout, kubiq reloads it, and the harness opens a session on the real
cluster: it grades the untouched namespace (every check must fail), runs the
drill's own solution in the candidate's shell, grades again (every check must
pass, inside the solve budget), ends the session and verifies the cluster is
back to baseline. Only a question that survives all of that is proven.

A critic then tries to refute each proven question from its record alone: the
task against the checks, the cited documentation, the curriculum. It rules
`confirmed`, `refuted` or `unproven`. Only `confirmed` is filed. Proofs and
rulings are kept under `~/.cache/swarmr-kubiq/`, keyed by the drill's content,
so a rerun proves and judges only what changed; a rerun on an unchanged exam is
a report and nothing else.

What a model cannot be trusted to get right, a tool refuses: weights that do not
mirror the curriculum shares, a drill that is a
rewording of one in the catalogue or of a sibling, a kind the cluster does not
serve, a check that can never pass, a solution that changes nothing, a private
key, a changed task on a fix round. The author fixes what the tool names.

## Prerequisites

- A running kubiq built from a checkout with `POST /api/reload`, whose compose
  file bind-mounts `drills/` and `exams/`, so a staged file is visible to the
  server as soon as it is written. The team never rebuilds or stops kubiq.
- `kubectl` on PATH, pointed at the cluster kubiq provisions sessions on.
- `lodestar` (MCP) for the Kubernetes documentation; unreachable degrades to
  empty searches, never to a failure.

## References

- [Architecture overview](./CLAUDE.md)
