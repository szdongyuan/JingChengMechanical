# Packaging Decision Checklist

Complete every item before generating deliverables.

## Decision Matrix

| Decision | Prefer this when | Tradeoff |
| --- | --- | --- |
| `one-shot` | file replay, manual debugging, and isolated runs matter most | startup cost is paid on every run |
| `resident` | imports are expensive, repeated requests are expected, and memory reuse matters | protocol handling is more complex |
| plain Python | target machines already have Python and fast iteration matters most | the runtime must be managed separately |
| Windows `.exe` | Python availability is uncertain and deployment simplicity matters more than size | builds are heavier and rebuilds are slower |
| `onedir` | practical default for ML payloads with sidecar weights or assets | ships as a directory instead of one file |
| `onefile` | optional when single-file delivery matters more than startup time | unpack startup cost is higher |

Windows `.exe` is a delivery format, not a protocol. One-shot and resident can
both remain plain Python or both ship as Windows `.exe`.

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
