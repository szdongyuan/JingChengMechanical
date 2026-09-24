---
name: subagent-driven-development
description: Use before implementing anything - dispatches fresh subagent per task with parallel dual review and a spec gate. As an orchestrator, you must never implement anything yourself.
---

# Subagent-Driven Development

Execute plan by dispatching a fresh subagent per task, then dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel after each implementation pass. `spec-code-reviewer` is the gate, but only after you have verified that its reported compliance mismatch is real and should be addressed in the current pass. For how reviews are dispatched, interpreted, triaged, validated, gated, and pushed back on, follow the `code-review` skill throughout the review cycle.

**Why subagents:** You delegate tasks to specialized agents with isolated context. By precisely crafting their instructions and context, you ensure they stay focused and succeed at their task. They should never inherit your session's context or history — you construct exactly what they need. This also preserves your own context for coordination work.

**Core principle:** Fresh subagent per task + parallel review pair (spec gate over quality, `code-review` skill) = high quality, fast iteration

**Completion rule:** Keep driving the plan forward until the entire plan is completed. Do not stop after one task, one review pass, or one fix loop. Only stop when all planned tasks are done or when you hit a real unresolved issue you cannot responsibly solve yourself, such as a required spec change, a missing human decision, a missing approval, or a verified blocker that invalidates the current execution path.

## Prerequisites

This skill requires two artifacts before starting:

1. **Spec** — a design document produced by the `writing-specs` skill, saved to `docs/double-sdd/specs/YYYY-MM-DD-<topic>-design.md`. If you don't have a spec, invoke `writing-specs` first.
2. **Plan** — an implementation plan produced by the `writing-plans` skill, saved to `docs/double-sdd/plans/YYYY-MM-DD-<feature-name>.md`. If you have a spec but no plan, invoke `writing-plans` first.

**IMPORTANT:** Do NOT assume these artifacts exist based on the topic being discussed or files you find by searching. A spec/plan only counts as "existing" if the user has **explicitly pointed you to it** (e.g., by providing the path directly). If the user has not explicitly indicated these files, treat them as missing and invoke the corresponding skill.

- No spec → invoke `writing-specs`, stop here
- Spec but no plan → invoke `writing-plans`, stop here
- Both exist → proceed with this skill

## Setup: Isolated Workspace

Before executing any tasks, create an isolated worktree from a recorded main-worktree base. This recorded base is the anchor used later to cleanly retain the final work without retaining temporary worktree commits.

```bash
# Ensure .worktrees/ is git-ignored
if ! git check-ignore -q .worktrees 2>/dev/null; then
  printf '\n.worktrees/\n' >> .gitignore
  git add .gitignore
  git commit -m "Add .worktrees to .gitignore"
fi

# Record the current main worktree state before any implementation commits
MAIN_BRANCH=$(git branch --show-current)
MAIN_BASE=$(git rev-parse --verify HEAD)
mkdir -p .worktrees
printf 'MAIN_BRANCH=%s\nMAIN_BASE=%s\nFEATURE_BRANCH=%s\n' "$MAIN_BRANCH" "$MAIN_BASE" "$BRANCH_NAME" > ".worktrees/$BRANCH_NAME.meta"

# Create worktree from the recorded base
git worktree add .worktrees/$BRANCH_NAME -b $BRANCH_NAME $MAIN_BASE
cd .worktrees/$BRANCH_NAME
```

Run project setup (npm install / cargo build / pip install / go mod download) and verify tests pass before proceeding.

Record `MAIN_BRANCH`, `MAIN_BASE`, the feature branch name, and the worktree path in your orchestration notes. Every later review range and final integration decision must be interpreted relative to this recorded base, not a guessed current branch state.

### Temporary Commit Policy

Commits made in the isolated worktree are temporary workflow checkpoints. Use them when they help define review ranges, preserve task boundaries, or recover from review loops, but do not treat them as permanent project history.

- Name temporary commits clearly, for example `tmp/sdd: task 2 parser validation`.
- Before each task, record the current worktree `HEAD` as that task's review base.
- After a task passes implementation and review, create or update a temporary checkpoint commit if needed so reviewers can inspect `TaskBase..HEAD`.
- Never merge the feature branch back with a normal merge that preserves these temporary commits.
- Final integration must squash the completed work onto `MAIN_BASE`, or soft-reset a mistakenly merged result back to `MAIN_BASE` before recommitting the final file state.

## The Process

1. Read plan, extract all tasks, create and maintain `update_plan` steps
2. **For each task, keep implementation sequential: never run multiple `implementer` subagents in parallel for the same plan. Reviewer subagents may run in parallel as described below.**
   a. Dispatch `implementer` subagent
      - After dispatching, let it work. Do not keep checking in unless it is blocked, needs approval, or has clearly failed.
   b. Handle implementer status:
      - NEEDS_CONTEXT → answer their questions, re-dispatch implementer from (a)
      - DONE / DONE_WITH_CONCERNS → continue to (c)
   c. Dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for the same git range
      - Treat each review as a single review pass, not an open-ended conversation.
      - Use the task's recorded pre-task `HEAD` as `Base` and the current temporary checkpoint as `Head`.
      - Wait for whichever review returns first instead of repeatedly polling both.
      - Before dispatching reviews, and again when review output returns, read and follow the `code-review` skill for review dispatch, triage, validation, spec gating, and reviewer pushback.
      - **5 consecutive review loops without both reviewers approving → stop loop, orchestrator assesses and decides next step**
      - Both approved → mark task complete, advance to next task
3. After all tasks complete: dispatch `quality-code-reviewer` for the **entire implementation** (full git range)
4. Invoke `finishing-a-development-branch` skill

## Dispatching Subagents

Three custom subagents, dispatched by name via `spawn_agent`:

**`implementer`** — Implements a single task using TDD.
Dispatch with: task description, spec/plan paths, working directory, context about dependencies.

**`spec-code-reviewer`** — Verifies whether the current implementation slice matches the requested spec-defined behavior and scope. This reviewer is the gate for the entire review pass.
Dispatch with: description, spec/plan paths, git range (Base..Head).

**`quality-code-reviewer`** — Reviews engineering quality for the current implementation slice.
Dispatch with: description, spec/plan paths, git range (Base..Head).

For every subagent dispatch in this skill:
- provide complete task context up front
- wait for the decisive result instead of repeatedly polling
- if more context is needed, answer once clearly and re-dispatch a fresh pass
- follow the `code-review` skill before dispatching reviews and when deciding whether spec feedback actually gates the pass

### Example Dispatch

```
spawn_agent:
  agent_type: implementer
  fork_context: false
  message: |
    Task 3: Implement user authentication endpoint

    Spec: docs/double-sdd/specs/auth-spec.md
    Plan: docs/double-sdd/plans/auth-plan.md
    Work from: .worktrees/feature-auth

    Context: This builds on the session middleware from Task 2.
    The database schema is already in place (see db/schema.ts).
```

```
spawn_agent:
  agent_type: spec-code-reviewer
  fork_context: false
  message: |
    Description: Task 3: user authentication endpoint

    Spec: docs/double-sdd/specs/auth-spec.md
    Plan: docs/double-sdd/plans/auth-plan.md
    Base: abc1234
    Head: def5678
```

```
spawn_agent:
  agent_type: quality-code-reviewer
  fork_context: false
  message: |
    Description: Task 3: user authentication endpoint

    Spec: docs/double-sdd/specs/auth-spec.md
    Plan: docs/double-sdd/plans/auth-plan.md
    Base: abc1234
    Head: def5678
```

## Handling Implementer Status

**DONE:** Proceed to the paired review dispatch.

**DONE_WITH_CONCERNS:** Read the concerns. If about correctness or scope, address before review. If observations (e.g., "this file is getting large"), note and proceed.

**NEEDS_CONTEXT:** Provide missing context and resume it.

**BLOCKED:** Assess the blocker:
1. If it's a context problem, provide more context and resume it
2. If the task is too large, break it into smaller pieces
3. If the plan itself is wrong, verify the blocker is real. Then:
      - If the fix is local to the plan (does not contradict the spec or invalidate completed tasks) → fix the plan yourself and re-dispatch
      - Otherwise → escalate to the human

**Never** ignore an escalation or force retry without changes. If the implementer said it's stuck, something needs to change.

## Evaluating Reviewer Feedback

Subagent reviewers catch real issues, but they also overreach and miss context. **Verify before implementing. Technical correctness over reviewer confidence.**

- Review output is input to evaluate, not an order to follow.
- If a reported compliance mismatch, required fix, or other review finding is real, still decide whether it should be fixed now, deferred, or skipped.
- For the full review-handling rules, read and follow the `code-review` skill.
- Keep the workflow moving until the full plan is complete unless you hit a real unresolved issue you cannot solve inside the current plan/spec boundary.

## Red Flags

**Never:**
- Implement the code yourself instead of using sub-agents.
- Start implementation on main/master branch without explicit user consent
- Start implementation before recording the main worktree base commit
- Preserve temporary worktree commits in the main branch history
- Use a normal merge as the default local integration path
- Skip reviews (spec compliance OR code quality)
- Proceed with unfixed issues
- Dispatch multiple implementation subagents in parallel (conflicts)
- Skip scene-setting context (subagent needs to understand where task fits)
- Ignore subagent questions (answer before letting them proceed)
- Accept "close enough" on spec compliance
- Skip review loops (reviewer found issues = implementer fixes = review again)
- Let implementer self-review replace actual review (both are needed)
- Treat code quality review as actionable before spec compliance passes
- Treat a raw reviewer `FAIL` as automatically decisive before verifying it
- Stop the workflow just because one review loop is inconvenient, noisy, or inconclusive
- Move to next task while either review gate has open issues

**If subagent asks questions:** Answer clearly and completely. Don't rush them.

**If reviewer returns `FAIL`:** verify the reported compliance mismatches or required fixes, send confirmed fixes back through the implementer, then re-review until approved.

**If subagent fails task:** Dispatch fix subagent with specific instructions. **Don't fix manually** (context pollution).

## Integration

**Required workflow skills:**
- **`writing-plans`** — Creates the plan this skill executes
- **`finishing-a-development-branch`** — Complete development after all tasks
