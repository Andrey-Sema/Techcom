# ADR-0008: `request_eta` is a first-class decision value

* Status: Accepted
* Date: 2026-10-21

## Context

Section B lists four classes; the example output and scenario S2 use
`request_eta` routed to a non-owner party.

## Decision

`DecisionKind` = no_action, follow_up_owner, request_eta,
clarify_missing_info, escalate. `request_eta` is documented as a routed
follow-up for material blockers.

## Consequences

+ Matches the brief's example output verbatim and makes S2 explicit.
− One more enum value than section B; explained in README.
