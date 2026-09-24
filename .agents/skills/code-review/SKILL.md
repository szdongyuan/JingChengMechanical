---
name: code-review
description: Parallel dual review via subagents — spec compliance gates code quality. Guides evaluation of reviewer feedback.
---

# Code Review

## Parallel Review Process

Every implementation gets two independent reviews for the same git range:

1. **Spec compliance** (`spec-code-reviewer` agent) — Did they build what was requested? Nothing more, nothing less.
2. **Code quality** (`quality-code-reviewer` agent) — Is it well-built? Clean, tested, production-ready.

Launch both reviewers in parallel, but treat the spec review as the gate:
- If `quality-code-reviewer` returns first, hold the result and wait for spec.
- If `spec-code-reviewer` returns first with `FAIL`, verify its reported compliance mismatches first. Only if you confirm a real spec compliance mismatch that should be addressed in the current pass should you ignore or discard any quality result from that pass and stop the still-running quality review if needed.
- Only if `spec-code-reviewer` passes may you consume the quality review result.
- After any code changes, re-run both reviews on the new diff.

## Dispatching Reviewers

Dispatch each reviewer as a subagent via `spawn_agent`, using the matching custom subagent plus an explicit `Description`, the spec/plan paths, and the git range:

```
spawn_agent:
  agent_type: spec-code-reviewer
  fork_context: false
  message: |
    Description: [task description]
    Spec: [path to spec]
    Plan: [path to plan]
    Base: [base SHA]
    Head: [head SHA]
```

```
spawn_agent:
  agent_type: quality-code-reviewer
  fork_context: false
  message: |
    Description: [task description]
    Spec: [path to spec]
    Plan: [path to plan]
    Base: [base SHA]
    Head: [head SHA]
```

Dispatch each review as a fresh, single-pass review. Do not let the review turn into a long interactive thread. If the reviewer lacks required context, gather it locally and re-dispatch a fresh review pass.

After dispatching both reviewers:
- wait for whichever one finishes first instead of polling both
- do not act on quality feedback until spec has passed
- if spec reports compliance mismatches first, verify them before deciding whether that pass is actually gated
- only after that verification should you close the active quality review for that pass and send fixes back through the implementer

## Evaluating Reviewer Feedback

Subagent reviewers are sharp, opinionated, sometimes insightful, sometimes biased, sometimes nitpicky, and sometimes wrong. **Verify before implementing. Technical correctness over compliance.**

### Review Output Is Not Ground Truth

- Review output is input to evaluate, not an order to follow.
- Report sections such as `Compliance Mismatches`, `Required Fixes`, `Non-Blocking Notes`, or other review findings are advisory until you verify them against the code.
- No reviewer wording automatically determines what you do next.
- Even a real compliance mismatch, required fix, or other review finding still needs an orchestrator decision: fix now, defer, or skip with a clear technical reason.

### Do Not Appease Reviewers

- Do not become submissive to reviewers.
- Do not send return work just to make the review loop end.
- Your job is not to satisfy the reviewer. Your job is to protect the codebase and keep the plan moving.
- Before sending anything back to the implementer, you must be able to state in plain engineering terms:
  - what is wrong now
  - why it matters now
  - why this pass should pay the cost to change it now
- If you cannot explain that concretely, do not send it back.

Treat every reported compliance mismatch, required fix, or other review finding as unverified until you confirm all of:
- the referenced code, file, API, or behavior actually exists
- the finding is real in this codebase
- the claimed impact matches the actual impact
- the proposed fix would not introduce regressions or unnecessary complexity
- the finding is worth acting on in the current pass instead of being deferred or skipped

### Reviewer approves

If spec approved, wait for or consume the paired quality result. If quality approved after spec approval, proceed to the next task.

### Reviewer reports compliance mismatches, required fixes, or findings

For each reported compliance mismatch, required fix, or finding, verify in this order:

1. **Reality check** — Does the referenced code, file, API, or behavior actually exist?
2. **Correctness check** — Is this actually wrong in this codebase, or just different from the reviewer's expectation?
3. **Impact check** — Is the reported impact real?
4. **Regression check** — Would the suggested fix break existing behavior, tests, or intentional design choices?
5. **Scope check** — Should this be fixed in the current pass, deferred, or skipped?

Then act:
- **Confirmed and worth fixing now:** fix it, test it, continue
- **Confirmed but not worth fixing in this pass:** defer it or skip it with a clear reason
- **Style preference / abstraction taste / speculative maintainability anxiety / cost-ineffective cleanup without concrete current-pass risk:** overrule it and move on
- **False positive / hallucination / overreach:** note the reason briefly and skip it
- **Unclear after inspecting the code:** investigate further before changing anything

If the failing reviewer is `spec-code-reviewer`, treat it as decisive only after you have verified that at least one reported spec compliance mismatch is real and should be addressed in the current pass. Until then, a raw reviewer `FAIL` is not yet a gate decision.

### Reviewer is blocked

- Gather the missing file/path/range or other concrete context
- Re-dispatch a fresh review pass with the missing context included
- Do not spend many turns chatting with the reviewer

### YAGNI Check

If reviewer suggests adding features or "implementing properly":
- Check codebase for actual usage
- If unused: skip (YAGNI)
- If used: implement

### When To Push Back

Push back when:
- Suggestion breaks existing functionality
- Reviewer lacks full context (common with subagents)
- Reviewer is overstating a low-impact finding
- Violates YAGNI
- Technically incorrect for this stack
- Conflicts with the plan or spec
- Adds complexity without enough product value

Note the technical reason, skip the suggestion, continue.

## The Bottom Line

**Subagent feedback = suggestions to evaluate, not orders to follow.**

Verify the finding exists. Verify the impact is real. Then decide whether to fix it now, defer it, or skip it.
