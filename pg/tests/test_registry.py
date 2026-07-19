"""Pure parsing tests for the registry tag helper (no network)."""
from k8ostester_pg.registry import _parse_ref, _version_key


def test_parse_ref_splits_registry_from_repo():
    assert _parse_ref("ghcr.io/cloudnative-pg/postgresql:16.4") == (
        "ghcr.io", "cloudnative-pg/postgresql")
    assert _parse_ref("ghcr.io/x/y@sha256:abc") == ("ghcr.io", "x/y")
    assert _parse_ref("postgres:16") == ("docker.io", "postgres")   # bare -> Docker Hub


def test_version_key_orders_like_a_version():
    tags = ["16.4", "16.10", "16.2", "17.0"]
    assert sorted(tags, key=_version_key, reverse=True) == ["17.0", "16.10", "16.4", "16.2"]
