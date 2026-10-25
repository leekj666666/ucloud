"""Frozen D5 controls; no evaluator labels or private observations."""

SINGLE_CALL = frozenset(
    {"B1", "A_D4_compact", "A_neutral_raw", "A_D4_raw", "A_D4_alias", "A_H1_off"}
)
VARIANTS = (
    frozenset({"B0", "B1", "B2", "B3", "H1_off", "H4_raw", "H4_alias", "H5_hybrid"})
    | SINGLE_CALL
)


def settings(variant):
    if variant not in VARIANTS:
        raise ValueError("Unregistered diagnostic variant")
    return {
        "view": "raw"
        if variant in {"B2", "H4_raw", "A_neutral_raw", "A_D4_raw"}
        else "compact_alias"
        if variant in {"H4_alias", "A_D4_alias"}
        else "compact",
        "prompt": "neutral"
        if variant in {"B1", "B2", "A_neutral_raw"}
        else "D4_H1_off"
        if variant in {"H1_off", "A_H1_off"}
        else "D4",
        "single_call": variant in SINGLE_CALL,
        "max_provider_attempts": 0
        if variant == "B0"
        else 1
        if variant in SINGLE_CALL
        else 4,
        "temperature": 0,
        "top_p": 1,
        "max_output_tokens": 8192,
        "model_seed_supported": False,
        "direct_observations_truncated": False if variant in SINGLE_CALL else None,
        "holmes_max_tool_content_characters": 32000
        if variant not in SINGLE_CALL
        else None,
    }
