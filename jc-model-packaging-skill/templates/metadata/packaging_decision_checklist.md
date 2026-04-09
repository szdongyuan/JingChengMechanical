# Packaging Decision Checklist

Complete every item before generating deliverables.

- [ ] Choose `one-shot` or `resident`.
- [ ] State why the chosen runtime shape fits the host.
- [ ] Decide whether weights stay `external` or are `embedded`.
- [ ] Choose plain Python or Windows `.exe`.
- [ ] If `.exe` is requested, explain why deployment simplicity matters more
      than package size or rebuild cost.
- [ ] If plain Python is enough, say so explicitly.
- [ ] Declare the duration policy.
- [ ] Confirm whether the duration strategy is `trim / pad`, `sliding window +
      aggregation`, `strict reject`, or `true variable-length`.
- [ ] If no stronger domain rule exists, use the default recommendation:
      fixed sample length, trim if longer, zero-pad if shorter.
- [ ] Record sample rate, channel layout, tensor shape, and normalization.
- [ ] Record preprocessing ownership.
- [ ] Record output interpretation.
- [ ] Declare expected artifact outputs and each artifact `relative_path`.
- [ ] Record verification commands for the chosen runtime shape.

Use `.exe` only as a delivery format. One-shot and resident modes can both
remain plain Python when that keeps deployment simpler.
