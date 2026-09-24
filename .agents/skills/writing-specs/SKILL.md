---
name: writing-specs
description: "You MUST use this before any implementation work. Clarifies requirements, explores approaches, validates the design, and writes the approved spec before planning."
---

# Writing Specs

Turn ideas into an approved design and written spec through collaborative dialogue.

<HARD-GATE>
Do NOT write code, scaffold anything, or invoke any implementation skill until you have:
1. explored the current project context
2. resolved material product questions
3. presented the design
4. received user approval

This applies to EVERY project regardless of perceived simplicity.
</HARD-GATE>

<QUESTION-GATE>
If any material product question is unresolved, stop and ask the user before moving forward.

Treat unresolved questions as blockers. This includes:
- missing behavior decisions
- unclear constraints or success criteria
- unknown edge cases or failure handling
- unresolved compatibility expectations
- ambiguous scope boundaries

Do NOT carry unresolved questions into approach selection, design approval, spec writing, review loops, or the handoff to `writing-plans`.

Surface these questions early so you can move into each next step with confidence, not silent doubts.
</QUESTION-GATE>

## Core Rules

- Every project goes through this flow, even if it looks simple. The design can be short, but it still must be explicit and approved.
- Ask one question at a time.
- If the request is too large for one spec, stop and help the user decompose it before continuing.
- Explicitly determine the compatibility posture before locking the design.
- The only terminal state of `writing-specs` is invoking `writing-plans`.

## Workflow

1. Explore the current project context: relevant files, docs, recent commits, and existing patterns.
2. Assess scope early. If the request spans multiple independent subsystems or deliverables, stop and decompose it with the user first.
3. Ask clarifying questions one at a time until purpose, constraints, success criteria, edge cases, and compatibility expectations are explicit.
4. Propose 2-3 approaches with trade-offs and recommend one.
5. Present the design in sections scaled to the complexity of the work and get user approval as you go.
6. If approval, spec writing, or review feedback exposes a new unresolved question, stop and return to the clarification loop.
7. Write the approved spec to `docs/double-sdd/specs/YYYY-MM-DD-<topic>-design.md`
8. Run the `spec-document-reviewer` loop until the spec is approved or you hit 5 iterations.
9. Ask the user to review the written spec.
10. If the user requests changes, update the spec and re-run review as needed.
11. After both the spec review loop and the user review pass, commit the approved spec document.
12. Invoke `writing-plans`.

## Understanding the Request

- Check the current repo before asking detailed questions.
- Prefer multiple-choice questions when possible, but use open-ended questions when needed.
- Momentum is never a reason to skip a needed question.
- If it is unclear whether backward compatibility matters, ask.

Compatibility posture must cover:
- whether backward compatibility is required, not required, or partial
- which surfaces are protected: API, CLI, config, file format, database schema, user-visible behavior, or other project-specific surfaces
- which breaking changes are acceptable, if any
- whether migration, compatibility layers, or deprecation support are required

## Designing

- Propose 2-3 approaches with clear trade-offs.
- Lead with your recommendation and explain why.
- Cover the level of detail the work actually needs: architecture, components, data flow, error handling, and testing.
- Design smaller units with clear responsibilities and interfaces.
- Follow existing codebase patterns unless the work requires targeted structural improvement.
- Do not introduce unrelated refactors.

## Writing the Spec

- Write the validated design to `docs/double-sdd/specs/YYYY-MM-DD-<topic>-design.md`
- User preferences for spec location override this default.
- Include a `Compatibility / Migration` section with:
  - `Backward compatibility: required | not required | partial`
  - `Protected surfaces: [...]`
  - `Allowed breakage: [...]`
  - `Migration strategy: none | compatibility layer | migration script | deprecation window | other`
- Do not hide unresolved questions behind `TBD`, `later`, or vague wording.
- If writing the spec exposes a new unresolved product decision, constraint, edge case, or compatibility question, stop and ask the user before continuing.

## Spec Review Loop

After writing the spec document:

1. Dispatch `spec-document-reviewer` via `spawn_agent`
2. Use:
   - `agent_type: spec-document-reviewer`
   - `fork_context: false`
3. Provide the spec file path and only the minimal review context needed.
4. Never pass your session history or chain-of-thought.
5. If the reviewer returns required clarifications that you can resolve with the current context, update the spec and re-dispatch.
6. If the reviewer returns a required clarification that depends on a new unresolved product question or compatibility requirement, stop and ask the user before continuing.
7. If the loop exceeds 5 iterations, surface it to the user.

## User Review Gate

After the spec review loop passes, ask the user to review the written spec before proceeding.

Use:

> "Spec written to `<path>`. Please review it and let me know if you want any changes before we write the implementation plan."

Wait for the user's response. If they request changes, update the spec and re-run review as needed.

## Commit Gate

After both the spec review loop and the user review pass, commit the approved spec document to git.

## Completion

The only terminal state of `writing-specs` is invoking `writing-plans`.
Do NOT invoke any other implementation skill after `writing-specs`.
