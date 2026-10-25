"""Evaluator-only causal case definitions, never mounted into an Agent."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Case:
    id: str
    split: str
    family: str
    target: str
    mechanism: str
    distractor: str | None = None
    tool_failure: bool = False
    expected_disposition: str = "recover"

    def public_description(self):
        return asdict(self)


DEV = (
    Case("dev_01", "dev", "config", "order", "missing_dns"),
    Case("dev_02", "dev", "config", "gateway", "missing_dns"),
    Case("dev_03", "dev", "route", "inventory", "numeric_port"),
    Case("dev_04", "dev", "route", "order", "numeric_port"),
    Case("dev_05", "dev", "dependency", "inventory", "zero_replicas"),
    Case("dev_06", "dev", "dependency", "order", "zero_replicas"),
    Case("dev_07", "dev", "resources", "inventory", "memory_oom"),
    Case("dev_08", "dev", "resources", "order", "memory_oom"),
    Case("dev_09", "dev", "config", "order", "missing_dns", "gateway"),
    Case("dev_10", "dev", "route", "inventory", "numeric_port", "order"),
    Case(
        "dev_11",
        "dev",
        "unsupported",
        "inventory",
        "selector_no_endpoints",
        expected_disposition="escalate",
    ),
    Case(
        "dev_12",
        "dev",
        "config",
        "order",
        "missing_dns",
        tool_failure=True,
        expected_disposition="escalate",
    ),
)
VALIDATION = (
    Case("val_01", "validation", "config", "order", "wrong_url_port", "inventory"),
    Case("val_02", "validation", "config", "gateway", "wrong_url_path"),
    Case("val_03", "validation", "route", "inventory", "unbound_named_port", "order"),
    Case("val_04", "validation", "route", "gateway", "numeric_port", "inventory"),
    Case("val_05", "validation", "dependency", "gateway", "zero_replicas", "order"),
    Case("val_06", "validation", "resources", "gateway", "memory_oom", "inventory"),
    Case(
        "val_07",
        "validation",
        "unsupported",
        "inventory",
        "selector_wrong_backend",
        expected_disposition="escalate",
    ),
    Case("val_08", "validation", "config", "order", "wrong_url_scheme"),
)


def cases(split):
    if split == "dev":
        return DEV
    if split == "validation":
        return VALIDATION
    raise ValueError("Formal holdout access is disabled during D5")


def holdout_configurations():
    # Only the sealing tool calls this; D5 capture/diagnosis reject this split.
    return [
        {
            "id": "holdout_" + str(index + 1).zfill(2),
            "split": "holdout",
            "family": family,
            "target": target,
            "mechanism": mechanism,
            "interference_pattern": pattern,
            "distractor_targets": sorted({"gateway", "order", "inventory"} - {target}),
            "semantics": "Held-out compound release contexts within known fault families; not unseen fault-class generalization",
        }
        for index, (family, target, mechanism, pattern) in enumerate(
            (family, target, mechanism, pattern)
            for family, target, mechanism in (
                ("config", "order", "missing_dns"),
                ("config", "gateway", "wrong_url_port"),
                ("route", "inventory", "unbound_named_port"),
                ("route", "order", "numeric_port"),
                ("dependency", "inventory", "zero_replicas"),
                ("dependency", "gateway", "zero_replicas"),
                ("resources", "inventory", "memory_oom"),
                ("resources", "order", "memory_oom"),
                ("unsupported", "inventory", "selector_wrong_backend"),
                ("config", "order", "wrong_url_scheme"),
            )
            for pattern in (
                "two_newer_unrelated_releases",
                "cross_namespace_and_local_release",
            )
        )
    ]
