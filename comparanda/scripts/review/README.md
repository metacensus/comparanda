# Review Scripts

These helpers support human review of high-leverage extracted claims. They are
not part of the normal inner-loop extraction path.

| Script | Use |
|---|---|
| `review_queue.py` | Select claims/fields that deserve reviewer attention. |
| `render_review_packet.py` | Produce a compact review packet with extraction values and cited bundle evidence. |
| `apply_review.py` | Apply reviewer decisions to extraction provenance with audit metadata. |

Reviewer edits must preserve provenance. Corrections should carry reviewer,
note, corrected quote/location/page when applicable, and superseded prior value.
