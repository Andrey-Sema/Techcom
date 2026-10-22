"""Export evidence with explicit provenance and known errors."""

from eval.harness import Evaluation


def markdown(result: Evaluation) -> str:
    """Render deterministic Markdown with explicit quality limits."""
    scores = result.scores
    lines = [
        "# Interpretation and drafting replay evaluation",
        "",
        f"Dataset: `{result.manifest.dataset_version}`.",
        "Prompts: `interp.v1`, `draft.v2`.",
        f"Provider quality: **{result.provider_quality}**.",
        "",
        result.manifest.provenance,
        "",
        (
            "Synthetic scripted responses verify workflow contracts "
            "and guardrails."
        ),
        (
            "They do not measure a live model's accuracy or attest "
            "provider origin."
        ),
        "No provider API was called. No API credentials were used.",
        "",
        f"- Examples: {scores.examples}",
        f"- Macro-F1 (seven classes): {scores.macro_f1:.6f}",
        f"- Failed-call fallback rate: {scores.fallback_rate:.6f}",
        f"- Model calls replayed: {scores.model_calls}",
        f"- Failed calls: {scores.failed_calls}",
        f"- Notes screened without model calls: {scores.screened_notes}",
        f"- Unsafe outcomes: {scores.unsafe_outcomes}",
        (
            f"- Decision mismatches against independent labels: "
            f"{scores.decision_mismatches}"
        ),
        f"- Contract gates: {', '.join(result.gate_failures) or 'PASS'}",
        "",
        (
            "Fallback rate counts rejected model calls, including "
            "hostile scripts;"
        ),
        "screening bypasses are reported separately and are not failed calls.",
        "A classification error is reported even when the final policy action",
        "still satisfies the independent safety invariants.",
        "",
        "| Class | Support | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {label.value} | {value.support} | "
        f"{value.precision:.6f} | {value.recall:.6f} | {value.f1:.6f} |"
        for label, value in scores.classes.items()
    )
    lines.extend(["", "## Disagreements", ""])
    disagreements = [
        row
        for row in result.results
        if row.expected != row.predicted
        or row.expected_decision != row.actual_decision
    ]
    if not disagreements:
        lines.append("No disagreements in this scripted corpus.")
    lines.extend(
        f"- `{row.case_id}`: label `{row.expected.value}` → "
        f"`{row.predicted.value}`; decision "
        f"`{row.expected_decision.value}` "
        f"→ `{row.actual_decision.value}`."
        for row in disagreements
    )
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"Dataset SHA-256: `{result.manifest.dataset_sha256}`.",
            f"Recordings SHA-256: `{result.manifest.recordings_sha256}`.",
            (
                "Every response binds its prompt instructions, "
                "schema and isolated input."
            ),
            (
                "`--require-provider` rejects this synthetic corpus. "
                "Captured provider"
            ),
            (
                "inputs must be supplied separately and their origin "
                "independently reviewed."
            ),
            "",
            "## Limits",
            "",
            (
                "Thresholds are contract regression gates: macro-F1 "
                "≥ 0.85, failed-call"
            ),
            (
                "fallback ≤ 0.10, unsafe outcomes = 0. Passing them "
                "here is not evidence"
            ),
            (
                "that a provider or prompt meets those quality "
                "thresholds on real notes."
            ),
        ]
    )
    return "\n".join(lines) + "\n"
