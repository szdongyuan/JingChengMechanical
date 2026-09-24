# Result History Single-Line Status Design

## Summary

Adjust the emphasized result-history conclusion so an NG result and its fault type appear on one line in the form `不通过：故障类型`.

## Scope

- Update only the emphasized overall status presentation in `ui/widgets/result_history_card.py` and directly affected tests.
- Keep the AI analysis table's complete-text wrapping and adaptive row fitting unchanged.
- Do not change result data contracts, history projections, manual judgment actions, or the fixed 360 px history-panel width.

## Approved Design

### NG status format

- Replace the parenthesized form `不通过（故障类型）` with `不通过：故障类型` wherever the result-history card builds or previews its overall NG status.
- When no fault type is available, continue to display `不通过` without a trailing colon.
- Keep the separate `结果摘要` caption and the existing `result_history_card_summary_overall` value label.

### Single-line adaptive typography

- Disable word wrapping on the emphasized overall value label.
- Keep 32 px and weight 800 as the default typography.
- Measure the complete status text against the label's actual available width and reduce its font size only when it would otherwise overflow.
- A normal four-Chinese-character fault type must remain at the default 32 px size at the production 360 px panel width.
- Clamp automatic reduction at a readable 18 px. If an unusually long configured fault type still exceeds the available width, visually elide the tail while preserving the complete value in the tooltip and accessible description.
- Recalculate the fitted display after construction, card-width changes, summary updates, and manual judgment previews.
- Keep the status label at the stable single-line minimum height used by the current large presentation; long text must not make the card taller.

## Status Semantics

- Terminal pass remains green, terminal fail/no-result remains red, and active/reanalyzing remains neutral.
- Plain-text rendering, object names, accessible names, and status widget identity remain unchanged.

## Verification

- Test that a four-character fault type renders exactly as `不通过：风扇异常`, stays on one line, and retains the 32 px style at a 360 px card width.
- Test that a longer status remains one line and reduces its font size rather than increasing label/card height.
- Test the minimum-size/elision fallback while verifying the tooltip retains the complete unelided status.
- Test that manual preview and in-place summary updates use the same colon formatting and recalculate the fitted font.
- Run the result-history card, result-history module, dashboard, and two-stage projection test files.

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces: [result data contracts, service signals, widget object names, accessible names, manual judgment behavior, fixed history-panel width, AI result table behavior]
- Allowed breakage: [overall NG punctuation changes from Chinese parentheses to a Chinese colon; overall status no longer wraps; overly long overall status may be visually elided after reaching 18 px]
- Migration strategy: none
