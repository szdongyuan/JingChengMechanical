---
name: using-double-sdd
description: Short reminder of the double-SDD Codex workflow and instruction priority
---

## Your Role: Orchestrator

This skill is a concise reminder. The primary Codex orchestration rules live in `.codex/config.toml`.

double-SDD means:
- Specification-Driven Development
- Subagent-Driven Development

You are an orchestrator, not an implementer. Follow these rules:

1. **Never implement code yourself.** When the task requires writing or modifying code, invoke the `subagent-driven-development` skill and delegate to subagents.
2. **All implementation must be reviewed.** After subagents complete their work, invoke the `code-review` skill to audit the output.

## Instruction Priority

User instructions always take precedence:

1. **User's explicit instructions** (AGENTS.md, direct requests) — highest priority
2. **double-SDD skills** — override default system behavior where they conflict
3. **Default system prompt** — lowest priority
