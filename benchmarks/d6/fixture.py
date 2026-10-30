"""Evaluator-only compound contexts; original-field cleanup is UID/version bound."""

import contextlib
import time
import uuid

from benchmarks.d5 import live
from benchmarks.d5.live import Fixture as BaseFixture
from benchmarks.d5.live import confirm_release_order


@contextlib.contextmanager
def namespace(value):
    original = live.KUBE
    live.KUBE = [*original[:-1], value]
    try:
        yield
    finally:
        live.KUBE = original


class Fixture(BaseFixture):
    def __init__(self, case, folder):
        super().__init__(case, folder)
        self.peer = None

    def inject(self):
        # Original fixture confirms the actual root and one strictly newer local release.
        super().inject()
        self.drain()
        time.sleep(1.1)
        pattern = self.case.interference_pattern
        if pattern == "two_newer_unrelated_releases":
            target = self.case.distractor_targets[1]
            self.env_change(target, "RELEASE_ID", "d6-" + uuid.uuid4().hex[:12])
            live.kube("rollout", "status", "deployment/" + target, "--timeout=120s")
            self.record["context_namespace"] = "sre-demo"
        elif pattern == "cross_namespace_and_local_release":
            self.peer = BaseFixture(self.case, self.folder / "peer")
            self.record["peer_fixture_folder"] = str(self.peer.folder)
            self.persist()
            with namespace("sre-demo-b"):
                self.peer.env_change(
                    self.case.distractor,
                    "RELEASE_ID",
                    "d6-cross-" + uuid.uuid4().hex[:12],
                )
                live.kube(
                    "rollout",
                    "status",
                    "deployment/" + self.case.distractor,
                    "--timeout=120s",
                )
            self.record["context_namespace"] = "sre-demo-b"
        else:
            raise ValueError("Unimplemented formal interference pattern")
        self.record["compound_context_complete"] = True
        self.persist()

    def confirm_context(self, records):
        if not self.record.get("compound_context_complete"):
            raise ValueError("Compound context missing")
        if self.case.interference_pattern == "cross_namespace_and_local_release":
            confirm_release_order(self.case, self, records)
            if not self.peer or not self.peer.record["active"]:
                raise ValueError("Cross-namespace context missing")
        else:
            revisions = records["releases"]["data"].get("release_revisions", [])
            # Both injected release values must appear on actual observed revisions.
            for change in self.record["changes"][1:]:
                if change["kind"] != "deployment" or not change["path"].endswith(
                    "/value"
                ):
                    continue
                if not any(
                    change["injected"]
                    in {c.get("env", {}).get("RELEASE_ID") for c in row["containers"]}
                    for row in revisions
                ):
                    raise ValueError(
                        "A formal unrelated rollout is absent from observed evidence"
                    )

    def cleanup(self):
        if self.peer and self.peer.record["active"]:
            with namespace("sre-demo-b"):
                self.peer.cleanup()
        return super().cleanup()
