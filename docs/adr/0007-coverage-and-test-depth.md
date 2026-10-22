# ADR-0007: 100 % branch coverage plus mutation testing

* Status: Accepted
* Date: 2026-10-21

## Context

The candidate requires 100 % coverage; reviewers value representative
tests over vanity metrics.

## Decision

Branch coverage gate at 100 %, no ad-hoc pragmas (CI grep), shared
contract tests for fakes and real adapters, property tests for invariants,
named scenario tests, mutmut on the domain (≥ 90 % killed).

## Consequences

+ Coverage cannot be gamed silently; fakes cannot drift.
− More test code than production code (expected ~1.5:1).
