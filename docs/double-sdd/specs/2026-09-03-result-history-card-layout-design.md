# Result History Card Layout Design

## Summary

Improve the result history card shown in the 360 px-wide result history panel so operators can recognize a failed result immediately and read the complete AI analysis result without relying on a tooltip.

## Scope

- Update only the result history card implemented by `ui/widgets/result_history_card.py`.
- Emphasize the overall result area, especially the `不通过` state.
- Make long AI analysis result text fully visible inside the summary table.
- Add or update focused widget tests in `tests/test_result_history_card.py`.
- Do not redesign the main `ResultModule`, result data contracts, analysis logic, or manual judgment controls.

## Approved Design

### Overall result area

- Replace the current small, left-aligned single-line summary treatment with a full-width, centered status area.
- Preserve the visible `结果摘要` context while making the actual result the dominant element.
- Render failed and no-result conclusions with a large, bold red result value of approximately 32 px.
- Render other conclusion states with the same stable geometry and their existing semantic colors so card height does not jump when a run changes phase.
- Allow the result value to wrap when it includes a long fault type, and size the area from its contents instead of fixing it to the old one-line height.
- Preserve the existing plain-text rendering, tooltip, accessible name, object name, and status-to-color meaning.

### AI analysis result

- Keep the existing four-column summary table and six summary rows.
- Allocate more horizontal space to the result column than the current equal-stretch layout.
- Enable wrapping for result cells and increase the AI row height when its result requires multiple lines.
- Recompute the fitted table height from actual row heights so all wrapped AI text remains visible and both scrollbars remain hidden.
- Apply the same sizing logic on initial construction and later in-place summary updates.
- Preserve the complete source result string in the table item and tooltip; do not abbreviate or discard fault information.

## Layout Behavior

- The result history module remains 360 px wide.
- The card may grow vertically to display the emphasized status and complete AI result.
- Non-AI rows remain compact unless their result content also requires wrapping.
- Manual judgment controls remain attached below the summary and continue to use their existing height animation.

## Data and Error Handling

- No data transformation or service changes are introduced.
- Missing summary rows continue to display the existing placeholders.
- Unknown result text continues to render as plain text using the neutral color behavior.
- Existing pass/fail/no-result color mapping remains authoritative.

## Verification

- Add a regression test proving the overall result uses the approved large, centered, wrapping presentation.
- Add a regression test at the production card width with a long AI failure result, proving the item keeps the full text, wrapping is enabled, the AI row grows, the fitted table contains all rows, and no scrollbar appears.
- Verify an in-place summary update recalculates the AI row and table height.
- Run the focused result history card tests, then the broader result history module tests.

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: [result history data contracts, service signals, widget object names, accessibility names, manual judgment behavior, fixed result-history panel width, user-visible non-result content]
- Allowed breakage: [the result history card may become taller; exact previous summary font size, one-line result height, equal column widths, and fixed 23 px AI row height are intentionally replaced]
- Migration strategy: none
