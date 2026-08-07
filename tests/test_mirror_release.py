import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from src import mirror_release


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _http_error(code: int, url: str, body: dict | None = None, location: str | None = None):
    headers = Message()
    if location:
        headers["Location"] = location
    payload = json.dumps(body or {}).encode("utf-8")
    return HTTPError(url, code, "redirect", headers, io.BytesIO(payload))


def _install_urlopen(monkeypatch, handler):
    """Route mirror_release's urlopen through ``handler(method, url)``."""
    calls: list[tuple[str, str]] = []

    def fake_urlopen(request, *args, **kwargs):
        calls.append((request.get_method(), request.full_url))
        result = handler(request.get_method(), request.full_url)
        if isinstance(result, HTTPError):
            raise result
        return _FakeResponse(json.dumps(result).encode("utf-8") if result is not None else b"")

    monkeypatch.setattr(mirror_release, "urlopen", fake_urlopen)
    return calls


def test_build_release_payload_marks_latest_and_target():
    payload = mirror_release.build_release_payload(
        tag="latest",
        title="Latest Events",
        notes="body",
        latest=True,
        prerelease=False,
        target_commitish="main",
    )
    assert payload["tag_name"] == "latest"
    assert payload["name"] == "Latest Events"
    assert payload["body"] == "body"
    assert payload["make_latest"] == "true"
    assert payload["prerelease"] is False
    assert payload["target_commitish"] == "main"


def test_build_release_payload_marks_non_latest_release():
    payload = mirror_release.build_release_payload(
        tag="dev",
        title="Development Events",
        notes="body",
        latest=False,
        prerelease=True,
        target_commitish=None,
    )
    assert payload["make_latest"] == "false"
    assert payload["prerelease"] is True
    assert "target_commitish" not in payload


def test_build_upload_url_escapes_asset_names():
    upload_url = "https://uploads.github.com/repos/octo/repo/releases/1/assets{?name,label}"
    built = mirror_release.build_upload_url(upload_url, "events nofpo.json")
    assert built == "https://uploads.github.com/repos/octo/repo/releases/1/assets?name=events%20nofpo.json"


def test_resolve_token_prefers_target_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "source-token")
    monkeypatch.setenv("TARGET_GITHUB_TOKEN", "target-token")
    assert mirror_release.resolve_token() == "target-token"


def test_resolve_token_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("TARGET_GITHUB_TOKEN", raising=False)
    with pytest.raises(mirror_release.GitHubApiError):
        mirror_release.resolve_token()


OLD = "https://api.github.com/repos/old-owner/mirror"
NEW = "https://api.github.com/repos/new-owner/mirror"


def test_request_json_follows_redirect_from_location_header(monkeypatch):
    def handler(method, url):
        if url == f"{OLD}/releases/1":
            return _http_error(307, url, location=f"{NEW}/releases/1")
        return {"ok": True}

    calls = _install_urlopen(monkeypatch, handler)
    result = mirror_release._request_json(
        method="DELETE", url=f"{OLD}/releases/1", token="t"
    )
    assert result == {"ok": True}
    assert calls == [("DELETE", f"{OLD}/releases/1"), ("DELETE", f"{NEW}/releases/1")]


def test_request_json_follows_redirect_from_body_url(monkeypatch):
    """GitHub's 307 for a renamed repo carries the target in the body, not Location."""
    target = "https://api.github.com/repositories/1057374905/releases/1"

    def handler(method, url):
        if url == f"{OLD}/releases/1":
            return _http_error(307, url, body={"message": "Moved Permanently", "url": target})
        return {}

    calls = _install_urlopen(monkeypatch, handler)
    mirror_release._request_json(method="DELETE", url=f"{OLD}/releases/1", token="t")
    assert calls[-1] == ("DELETE", target)


def test_request_json_gives_up_after_max_redirects(monkeypatch):
    def handler(method, url):
        return _http_error(307, url, location=url + "/again")

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(mirror_release.GitHubApiError, match="exceeded"):
        mirror_release._request_json(method="DELETE", url=f"{OLD}/releases/1", token="t")


def test_request_json_still_raises_on_non_redirect_errors(monkeypatch):
    def handler(method, url):
        return _http_error(422, url, body={"message": "Unprocessable"})

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(mirror_release.GitHubApiError, match="HTTP 422"):
        mirror_release._request_json(method="POST", url=f"{OLD}/releases", token="t", payload={})


def test_resolve_repo_returns_renamed_full_name(monkeypatch):
    _install_urlopen(monkeypatch, lambda method, url: {"full_name": "new-owner/mirror"})
    assert mirror_release.resolve_repo("old-owner/mirror", "t") == "new-owner/mirror"


def test_resolve_repo_raises_when_missing(monkeypatch):
    _install_urlopen(monkeypatch, lambda method, url: _http_error(404, url))
    with pytest.raises(mirror_release.GitHubApiError, match="not found"):
        mirror_release.resolve_repo("old-owner/mirror", "t")


def test_sync_release_targets_the_renamed_repository(monkeypatch, tmp_path):
    asset = tmp_path / "events.json"
    asset.write_text("[]", encoding="utf-8")

    def handler(method, url):
        if url == OLD:
            return {"full_name": "new-owner/mirror"}
        if url.endswith("/releases/tags/latest"):
            return {"id": 7}
        if method == "POST" and url.endswith("/releases"):
            return {"upload_url": f"{NEW}/releases/7/assets{{?name,label}}"}
        return {}

    calls = _install_urlopen(monkeypatch, handler)
    mirror_release.sync_release(
        repo="old-owner/mirror",
        tag="latest",
        title="Latest Events",
        notes="body",
        assets=[asset],
        latest=True,
        prerelease=False,
        target_commitish="main",
        token="t",
    )

    urls = [url for _, url in calls]
    # Only the initial rename lookup may reference the old name.
    assert urls[0] == OLD
    assert all(url.startswith(NEW) for url in urls[1:])
    assert ("DELETE", f"{NEW}/releases/7") in calls
    assert ("POST", f"{NEW}/releases/7/assets?name=events.json") in calls
