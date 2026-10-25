# Model value: the same corpus with and without the model

Dataset: `luna-high.v1`.
Responses: `gpt-6-luna`; provider quality **CAPTURE_REPLAY_ONLY**.

Without the model, notes go through keyword hits, the `unclear`
fallback and catalogue templates: the service's offline path.
Policy, routing and the safety judge are identical in both rows.

| Pipeline | Macro-F1 | Benign macro-F1 | Wrong decisions | Unsafe outcomes |
|---|---:|---:|---:|---:|
| Without the model | 0.338 | 0.284 | 35 | 0 |
| With the model | 0.991 | 0.984 | 0 | 0 |

## Decisions the model changed

- Fixed by the model: 35
- Broken by the model: 0
- Wrong either way: 0

- `mat-parts`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `upstream-crew`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `upstream-release`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `permit-signoff`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `permit-letter`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `access-key`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `access-escort`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-weather`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-equipment`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `mat-reel`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `mat-connectors`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `mat-spanish`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `mat-unicode`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `mat-negation`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `mat-backorder`: `follow_up_owner` → `request_eta` (label `request_eta`, fixed by the model).
- `upstream-inspection`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `upstream-survey`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `upstream-spanish`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `upstream-notpermit`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `upstream-unicode`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `permit-license`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `permit-unicode`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `permit-spanish`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `permit-negation`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `access-badges`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `access-spanish`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `access-unicode`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `access-notpermit`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-rain`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-wind`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-illness`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-inspection`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-spanish`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-unicode`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
- `other-negation`: `follow_up_owner` → `escalate` (label `escalate`, fixed by the model).
