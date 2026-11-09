"""Fork guarantee: nothing is uploaded to the upstream author's servers by default."""

from unittest.mock import patch

from app.services import contribution_uploader as cu


def test_kill_switch_off_by_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_UPSTREAM_UPLOADS", raising=False)
    assert not cu.upstream_uploads_allowed()


async def test_drain_never_opens_a_client(monkeypatch):
    monkeypatch.delenv("ENGRAM_UPSTREAM_UPLOADS", raising=False)
    uploader = cu.ContributionUploader()
    with patch.object(cu.httpx, "AsyncClient") as client:
        assert await uploader._drain() == 0
    client.assert_not_called()


def test_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ENGRAM_UPSTREAM_UPLOADS", "1")
    assert cu.upstream_uploads_allowed()
