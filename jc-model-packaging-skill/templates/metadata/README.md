# Metadata Templates

Fill out these metadata files before you touch the wrapper template.

## What To Record

- model entrypoint ownership
- sample rate and channel contract
- tensor shape
- normalization assumptions
- preprocessing ownership and ordered preprocessing steps
- output interpretation
- duration mismatch policy
- delivery mode choice: one-shot, resident, or optional `.exe`

## Minimum Workflow

1. Copy `model_contract.template.json` and replace every placeholder.
2. Record the chosen duration policy before generating code.
3. Complete `packaging_decision_checklist.md`.
4. Use the finished metadata as the source of truth when adapting the wrapper.

Do not rely on hidden repository context. The metadata should be sufficient for
another agent to understand the model contract.
