---
title: Delegating implementation to headless CLI coding agents
date: 2026-09-02
category: workflow-issues
module: multi_agent_delegation
problem_type: workflow_issue
component: development_workflow
severity: high
related_components:
  - tooling
  - testing_framework
  - infrastructure
applies_when:
  - Delegating implementation or review work to an external CLI coding agent in headless mode
  - Running an agent inside a git worktree whose metadata lives in the parent repo .git directory
  - Running two or more agents in parallel on the same unit of work for comparison
  - Needing a review agent that can read and run everything but must not mutate the real branch
  - Accepting an agent self-report such as "all findings fixed" or "all tests pass" as done
symptoms:
  - "`grok -p` exits 0 in headless mode having read a few files and written nothing"
  - "`codex exec` inside a worktree cannot commit: Unable to create .git/worktrees/<name>/index.lock"
  - Agent stdout piped through tail shows no interim progress until the process exits
  - "Restricting a reviewer with `--disallowed-tools` causes another silent no-op run"
  - Two agents report conflicting conclusions about the same design decision
root_cause: missing_permission
resolution_type: workflow_improvement
tags:
  - headless-agents
  - cli-agents
  - git-worktree
  - sandbox-permissions
  - agent-delegation
  - verification
  - codex
  - grok
---

# Delegating implementation to headless CLI coding agents

## Context

The U2 "Local Ledger Core" unit was delegated to external CLI coding agents (`codex`, `grok`) running on this machine, orchestrated from Claude Code through the Bash tool. Each implementing agent got its own `git worktree` on its own branch and a written brief; reviews were then run by separate agents against the resulting code.

The mechanics of the delegation, not the ledger design, cost the most time. Three failures showed up in sequence, none of which announced itself as a failure:

- A headless `grok` run exited 0 having read the brief and a handful of source files, and produced nothing: no edits, no commit, clean worktree. It burned two runs before diagnosis and a third after a reviewer run was locked down with `--disallowed-tools`.
- A sandboxed `codex exec` implemented its task correctly and then could not commit it.
- Backgrounded agents piped through `tail` produced an empty log for their entire runtime, so a working run was indistinguishable from a stalled one.

The work described here lives on two unmerged local branches, `u2-ledger-codex` and `u2-ledger-grok`, neither merged to `main`. `main` is at the merge of PR #9.

## Guidance

### Give headless agents full permissions, and control blast radius with the filesystem instead

In headless (`-p` / `--single`) mode there is no human to approve a tool call. `--permission-mode acceptEdits` covers file edits but not the Bash calls an agent needs to run the test suite or commit — so the agent reads the brief, hits its first blocked shell call, and stops. Exit code 0. Nothing written.

Do not do this:

```bash
grok -p "<prompt>" -m grok-4.6 --reasoning-effort medium \
  --cwd <dir> --permission-mode acceptEdits --output-format plain
```

Do this:

```bash
grok -p "<prompt>" -m grok-4.6 --reasoning-effort medium \
  --cwd <dir> --permission-mode bypassPermissions --always-approve \
  --max-turns 150 --output-format plain
```

The same rule applies to a reviewer that must not touch the real branch. Restricting its tools reproduces the silent no-op exactly — a reviewer still needs to run the suite and script reproductions. Give it full permissions pointed at a throwaway copy:

```bash
cp -R <worktree> /tmp/review-copy
grok -p "<prompt>" ... --cwd /tmp/review-copy --permission-mode bypassPermissions
```

Containment becomes a property of the directory, not of the permission flags.

### The Codex sandbox excludes every `.git` from writes; name it explicitly

Codex's `workspace-write` sandbox carves `.git/` out of every writable root — the primary workspace and any `--add-dir` alike. Verified with `codex sandbox`: a file in the repo root is writable, a file in that repo's `.git/` is `Operation not permitted`, and it becomes writable only when `.git` is itself named as a writable root. Any git command that writes the index or refs fails at the commit step, after all the real work is done:

```text
fatal: Unable to create '<repo>/.git/index.lock': Operation not permitted
```

This is not specific to worktrees, although a worktree makes it more confusing: its `.git` is a file pointing at `<parent>/.git/worktrees/<name>/`, so the path in the error lives in the parent repo. The cause is the same carve-out either way. Adding the repo directory alone does not help — one run was lost to exactly that. Name the `.git` for every repo the agent must commit in; for a worktree, that is the parent's:

```bash
codex exec -m gpt-5.6-sol -c model_reasoning_effort="medium" \
  --sandbox workspace-write \
  --add-dir <repo>/.git \
  --skip-git-repo-check \
  -C <worktree> \
  -o <last-message-file> \
  "<prompt>"
```

### Never pipe a backgrounded agent through `tail`

`... 2>&1 | tail -40` buffers until stdin closes, so the output file stays empty for the whole run and there is no interim signal. Use the agent's own final-message flag, which writes to a known path independently of the pipe: `codex exec -o <file>`. Redirect the full stream to a file if you want progress and read the tail of the file yourself.

### Check auth from the context you will spawn from (session history)

An external CLI agent spawned headless can fail on auth purely because it inherits a different session or keychain context than the one where login happened. A prior session on this machine traced a "not logged in" failure to exactly this: the Claude CLI picks the macOS login Keychain or a plaintext fallback by probing `security show-keychain-info`, and under SSH the locked keychain sent a fresh token to the plaintext file while the GUI session kept reading a stale Keychain entry. Grok's credential here is OAuth/browser-established rather than an env API key, so it depends on a pre-established file being readable in whatever context the orchestrator spawns from. Verify auth from the spawning context, not from the interactive shell.

Related class of failure from the same session: `security add-generic-password` blocked waiting on a TTY when its input was piped. Expect the same from any delegated CLI that assumes a TTY.

### Hand over a written brief, not a prompt

Both implementing agents were pointed at a Markdown handoff document rather than given a long inline prompt. The brief carried the objective, a ranked reading list with per-file reasons, verified current state, the settled design decisions, and explicit corrections to stale plan references. Review findings were likewise written to a file — ranked most severe first, each with a reproduction — and handed to the fixing agent by path.

Two properties make this worth the extra step: the same brief is reusable across agents without re-typing, and the invocation stays short enough to read.

### Never accept a self-report

Re-derive every claim. When the fixing agent reported "68 tests passed" and listed fourteen findings addressed, re-running the suite in its worktree confirmed the count, and four of the highest-severity findings were reproduced by hand before the claim was believed. Dependency additions were checked by parsing imports rather than by reading the summary.

### Run reviewers in parallel and keep their disagreements

Two independent reviews of the same commit each found a real defect the other missed. Where two reviewers disagreed about whether a behavior was intentional, surfacing the disagreement beat picking a winner — the resolution was a product decision, not a technical one.

## Why This Matters

The three failure modes share a shape: the agent exits 0 and the orchestrator has no signal. A silent no-op looks exactly like a run whose work is still pending. Without knowing why, the natural response is to re-run with a slightly different prompt, which is how two runs became five.

The permission finding inverts an intuition worth naming. Locking a headless agent down does not make it a safer version of itself; it makes it a broken version of itself, and the breakage is invisible. Isolation belongs at the filesystem level — a worktree per implementer, a copy per reviewer — where an agent with full permissions still cannot damage anything that matters.

The verification discipline paid for itself in a domain where it has to. This repo handles bank credentials and local financial data, and its stated principles are local-first with no credentials in Git. Four of the fourteen review findings were classified as data loss, two of which destroyed data the user already had; every one was found by running a reproduction rather than by reading the diff. The sharpest was a case-folding path alias: on a case-insensitive filesystem, two `Path` objects that compare unequal can point at one inode, so a CSV export replaced the JSONL ledger and exited 0. Divergent reviews are what surfaced it. A single reviewer, however good, gives you one traversal of the code.

## When to Apply

- Any invocation of an external CLI coding agent in headless or non-interactive mode.
- Any agent running inside a `git worktree` under a sandbox, whether or not it is expected to commit — the failure lands at commit time, after the work.
- Any backgrounded agent whose progress you intend to watch.
- Any time two or more agents work the same unit for comparison, or a reviewer must read and execute but not mutate the real branch.
- Any time an agent reports "all findings fixed" or "all tests pass".

Skip the worktree-per-agent setup for single-agent work on a throwaway branch; the `.git` writable-set fix still applies if a sandbox is involved.

## Examples

### Reviewer isolation

Before — restricted tools, one more silent no-op:

```bash
grok -p "Review the ledger implementation..." -m grok-4.6 \
  --cwd <worktree> \
  --disallowed-tools "edit,write" --permission-mode acceptEdits
# exit 0, no findings, no evidence it ran anything
```

After — full permissions on a disposable copy:

```bash
cp -R <worktree> /tmp/review-copy
grok -p "Review ... write findings to /tmp/review-copy/findings.md" -m grok-4.6 \
  --cwd /tmp/review-copy \
  --permission-mode bypassPermissions --always-approve \
  --max-turns 150 --output-format plain
```

The reviewer can now run the suite, script a reproduction, and write scratch files. The real branch is untouched because it is not there.

### Findings as an artifact, not a prompt

Before: paste fourteen findings into the fixing agent's prompt.

After: write them to a findings file, ranked, each with a reproduction and a stated decision where one was needed, then tell the agent to read the file. The reproduction line is what makes the fix checkable afterward: the same command, re-run by hand, is the verification.

### Residual findings are the output of verification, not of the fix run

After the fixing agent claimed all fourteen closed, independent verification passes confirmed the fourteen and produced three new residuals plus one benign judgement call, each reproduced by execution — including that a fix from the first round was itself over-broad. It was only found because the fix was re-derived rather than accepted.

## Related

- `docs/plans/2026-07-02-001-feat-product-iterations-plan.md` — the plan whose U2 unit was the payload. Its U2 section still presents completed work as pending, and its U1 file list names two paths that do not exist.
- `docs/explainers/2026-09-01-u2-local-ledger-core.html` — untracked companion explaining what was built, where this doc explains how the agents were driven.
- GitHub issue #2 "Iteration 1: Introduce the local ledger core" — still open; the implementation lives on an unmerged branch.
