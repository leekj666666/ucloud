"""Fail-closed projection of independently observed verification feedback.

This experimental component does not enable another worker round by itself.
Only fixed check names and booleans can reach a future diagnostic request.
"""

BUSINESS_CHECKS = frozenset(
    {
        "all_requests_succeeded",
        "all_invariants_succeeded",
    }
)
REQUIRED_SAFETY_CHECKS = frozenset(
    {
        "duration_at_least_120",
        "all_scheduled_requests_recorded",
        "business_checks_at_least_12",
        "monitoring_available",
        "no_new_related_severe_alert",
        "resource_samples_present",
        "traffic_schedule_preserved",
    }
)


def feedback_for_reinvestigation(quality, *, action_stable, attempts, stopped):
    """Return limited business observations, or None when escalation is required.

    Budget and stability are explicit inputs from the authoritative workflow;
    this helper cannot grant execution or approve a proposed action.
    """
    if (
        stopped
        or action_stable is not True
        or type(attempts) is not int
        or attempts != 0
    ):
        return None
    if not isinstance(quality, dict) or quality.get("passed") is not False:
        return None
    checks = quality.get("checks")
    if not isinstance(checks, dict):
        return None
    if any(checks.get(name) is not True for name in REQUIRED_SAFETY_CHECKS):
        return None
    if any(type(checks.get(name)) is not bool for name in BUSINESS_CHECKS):
        return None
    failed = sorted(name for name in BUSINESS_CHECKS if checks[name] is False)
    if not failed:
        return None
    return {
        "source": "independent_business_verification",
        "passed": False,
        "failed_checks": failed,
        "instruction_authority": False,
    }


def feedback_for_warmup(samples, *, action_stable, attempts, stopped):
    """Accept only repeated, cleaned business failures with available monitoring."""
    if (
        stopped
        or action_stable is not True
        or type(attempts) is not int
        or attempts != 0
    ):
        return None
    if not isinstance(samples, list) or len(samples) < 3:
        return None
    failures = []
    for entry in samples[-3:]:
        if not isinstance(entry, dict) or not isinstance(entry.get("monitoring"), dict):
            return None
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("passed") is not False:
            return None
        if any(
            result.get(key)
            for key in ("error_type", "archive_error_type", "cleanup_error_type")
        ):
            return None
        if result.get("cleanup", {}).get("cleaned") is not True:
            return None
        checks = result.get("checks")
        if not isinstance(checks, dict):
            return None
        failed = sorted(
            k
            for k, v in checks.items()
            if k
            in {
                "confirmed",
                "payload_matches",
                "readback",
                "idempotent",
                "stock_delta",
                "payload_conflict",
                "conflict_stock_unchanged",
            }
            and v is False
        )
        if (
            not failed
            and result.get("check") == "create_order"
            and type(result.get("http_status")) is int
            and 500 <= result["http_status"] <= 599
        ):
            failed = ["create_order_http_5xx"]
        if not failed:
            return None
        failures.append(failed)
    if any(failure != failures[0] for failure in failures):
        return None
    return {
        "source": "repeated_business_warmup",
        "passed": False,
        "failed_checks": failures[0],
        "instruction_authority": False,
    }
