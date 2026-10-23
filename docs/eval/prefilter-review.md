# Independent review of conservative blocker deferral

Reviewed change: `7b8b896b8a063c8ea0eb39c17921aa2a786bc3c1`, specifically
`domain/interpretation.py`, against the merged application pipeline
`9496821150e461d8c78284e7cff1bb5406822058`.

The change defers ambiguous keyword matches to interpretation. It returns no
keyword interpretation; it does not assert a `NONE` blocker or decide an action.
`NoteInterpreter` still screens notes before keyword classification. A flagged
note stays out of the model. With an unavailable model, a deferred note becomes
`UNCLEAR`; dates, status and missing facts still determine a human follow-up.
The model's schema still excludes status, owner, deadline, recipient and actions.

No new structural safety bypass was found in this focused review. Hypothesis
checks in `tests/eval/test_prefilter_review.py` verify that resolution variants
never become automatic `NONE`, unrelated crew arrival/completion preserves a
locked-gate hit, and ambiguity cues do not bypass the injection screen.
The full post-fix replay independently checks routing and message boundaries.
The regex uses bounded resolution spans and no nested unbounded quantifiers;
source notes are already bounded before matching.

The intended tradeoff remains visible: broad words such as `resolved` and
`quotes` defer the whole note, including mixed current and historical blockers.
That can increase model calls and conservative owner follow-up when the model
is unavailable. The heuristic is not a complete negation or history parser.
It cannot establish that a valid model interpretation is semantically correct.
Passing structural safety checks therefore does not establish correct routing.

The post-fix report adds three synthetic `NONE` interpretations for unchanged
independent benign labels. It demonstrates the new call path and correction
under scripted inputs. It provides no evidence of live-provider quality.
Pre-fix reports remain archived; they are not presented as executions of this
changed pipeline. The empty-note classification disagreement remains disclosed.
