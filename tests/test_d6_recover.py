import pytest

from benchmarks.d6 import recover


def test_recovery_rejects_foreign_path_before_any_docker_or_cluster_access(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(recover, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(
        recover.subprocess,
        "check_output",
        lambda *a, **k: pytest.fail("Docker access before path validation"),
    )
    with pytest.raises(ValueError, match="outside exact D6"):
        recover.restore(tmp_path / "outside" / "e2e-untrusted")
