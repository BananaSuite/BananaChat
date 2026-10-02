"""Static releases must update cached browsers and keep ES modules coherent."""

from __future__ import annotations

import os
import re
from urllib.parse import urljoin

import pytest
from flask import url_for

from tests.app.test_chat import new_chat


def _asset_url(app, filename):
    with app.test_request_context():
        return url_for("static", filename=filename)


def _page_assets(html):
    return re.findall(r'(?:href|src)="(/static/[^" ]+)"', html)


def test_content_revision_is_independent_of_creation_order_and_mtime(tmp_path):
    from bananachat.assets import revision

    files = {"js/core.js": b"export const ready = true;", "css/app.css": b"body { color: black; }",
             "img/favicon.png": b"example image"}
    roots = [tmp_path / "first", tmp_path / "second"]
    for index, root in enumerate(roots):
        names = list(files) if index == 0 else list(reversed(files))
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    assert revision(roots[0]) == revision(roots[1])
    assert re.fullmatch(r"[0-9a-f]{20}", revision(roots[0]))


def test_content_revision_changes_when_bytes_change_with_preserved_mtime(tmp_path):
    from bananachat.assets import revision

    path = tmp_path / "core.js"
    path.write_bytes(b"export const warning = true;")
    original_stat = path.stat()
    before = revision(tmp_path)
    # ZIP extraction and reproducible builds can preserve both timestamps and size.
    path.write_bytes(b"export const warning = null;")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert path.stat().st_size == original_stat.st_size
    assert revision(tmp_path) != before


def test_content_revision_changes_when_a_file_is_renamed(tmp_path):
    from bananachat.assets import revision

    path = tmp_path / "core.js"
    path.write_text("export const ready = true;")
    before = revision(tmp_path)
    path.rename(tmp_path / "chat.js")
    assert revision(tmp_path) != before


def test_new_html_avoids_previously_cached_unversioned_asset_urls(app):
    response = app.test_client().get("/login")
    assert response.status_code == 200
    assets = _page_assets(response.get_data(as_text=True))
    assert assets
    assert _asset_url(app, "js/core.js") in assets
    assert _asset_url(app, "css/app.css") in assets
    assert all(re.match(r"/static/v[0-9a-f]{20}/", url) for url in assets)
    assert "/static/js/core.js" not in assets
    assert "/static/css/app.css" not in assets


def test_new_release_changes_html_asset_urls_when_mtime_is_preserved(make_app, tmp_path, monkeypatch):
    from bananachat import assets

    asset_tree = tmp_path / "release-assets"
    asset_tree.mkdir()
    source = asset_tree / "core.js"
    source.write_bytes(b"export const warning = true;")
    original_stat = source.stat()
    original_revision = assets.revision
    monkeypatch.setattr(assets, "revision", lambda: original_revision(asset_tree))
    first = make_app()
    old_urls = _page_assets(first.test_client().get("/login").get_data(as_text=True))

    source.write_bytes(b"export const warning = null;")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = make_app()
    new_urls = _page_assets(second.test_client().get("/login").get_data(as_text=True))

    assert old_urls and new_urls
    assert set(old_urls).isdisjoint(new_urls)
    assert _asset_url(first, "js/core.js") != _asset_url(second, "js/core.js")


def test_chat_relative_imports_resolve_to_the_same_core_module_as_the_html(app, admin):
    response = admin.get(f"/chat/{new_chat(admin)}")
    assert response.status_code == 200
    assets = _page_assets(response.get_data(as_text=True))
    core_url = _asset_url(app, "js/core.js")
    chat_url = _asset_url(app, "js/chat.js")
    assert core_url in assets and chat_url in assets

    # Traverse the actual import graph. A query-only version on <script> would
    # instantiate a second core module when chat.js imports ./core.js.
    pending, visited, core_imports = [chat_url], set(), []
    while pending:
        module_url = pending.pop()
        if module_url in visited:
            continue
        visited.add(module_url)
        module = admin.get(module_url)
        assert module.status_code == 200, module_url
        imports = re.findall(r'\bfrom\s+["\'](\./[^"\']+)["\']', module.get_data(as_text=True))
        for name in imports:
            imported_url = urljoin(module_url, name)
            assert imported_url.startswith(f"{app.static_url_path}/")
            if name == "./core.js":
                core_imports.append(imported_url)
            pending.append(imported_url)

    assert len(core_imports) >= 3
    assert set(core_imports) == {core_url}


@pytest.mark.parametrize("filename,mime", [
    ("js/core.js", "text/javascript"),
    ("css/app.css", "text/css"),
    ("img/favicon.png", "image/png"),
])
def test_canonical_static_assets_keep_correct_mime_and_cache_policy(app, filename, mime):
    response = app.test_client().get(_asset_url(app, filename))
    assert response.status_code == 200
    assert response.mimetype == mime
    assert response.cache_control.public
    assert response.cache_control.max_age == 12 * 60 * 60
    assert not response.cache_control.no_cache
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("filename,mime", [
    ("js/core.js", "text/javascript"),
    ("css/app.css", "text/css"),
    ("img/favicon.png", "image/png"),
])
def test_legacy_static_assets_revalidate_instead_of_reusing_twelve_hour_cache(app, filename, mime):
    browser = app.test_client()
    response = browser.get(f"/static/{filename}")
    canonical = browser.get(_asset_url(app, filename))
    assert response.status_code == 200
    assert response.mimetype == mime
    assert response.data == canonical.data
    assert response.cache_control.no_cache
    assert response.cache_control.max_age == 0
    assert "Expires" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    revalidated = browser.get(f"/static/{filename}", headers={"If-None-Match": response.headers["ETag"]})
    assert revalidated.status_code == 304
    assert revalidated.cache_control.no_cache and revalidated.cache_control.max_age == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_static_assets_are_available_before_setup_and_without_a_session(make_app, legacy):
    app = make_app(setup=False)
    browser = app.test_client()
    url = "/static/js/core.js" if legacy else _asset_url(app, "js/core.js")
    response = browser.get(url)
    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert "Location" not in response.headers
    assert "Set-Cookie" not in response.headers

    # Setup itself must reference the canonical release so it is styled and usable.
    setup = browser.get("/setup")
    assert setup.status_code == 200
    assert _asset_url(app, "js/core.js") in _page_assets(setup.get_data(as_text=True))


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("relative", ["../private.txt", "%2e%2e/private.txt", "..%2fprivate.txt"])
def test_static_paths_cannot_read_a_private_file_outside_the_static_folder(app, tmp_path, legacy, relative):
    public = tmp_path / "public"
    public.mkdir()
    secret = b"private instance data must not be served"
    (tmp_path / "private.txt").write_bytes(secret)
    app.static_folder = str(public)
    prefix = "/static" if legacy else app.static_url_path

    response = app.test_client().get(f"{prefix}/{relative}")
    assert response.status_code == 404
    assert secret not in response.data


def test_authenticated_html_still_cannot_be_cached_after_asset_versioning(admin):
    response = admin.get("/account")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.cache_control.private
    assert response.cache_control.no_store
    assert not response.cache_control.public


@pytest.mark.parametrize("setup,url", [(True, "/login"), (False, "/setup")])
def test_public_html_loads_a_fresh_asset_graph_and_csrf_nonce(make_app, setup, url):
    browser = make_app(setup=setup).test_client()
    first, second = browser.get(url), browser.get(url)
    for response in (first, second):
        assert response.status_code == 200
        assert response.mimetype == "text/html"
        assert response.cache_control.private and response.cache_control.no_store
        assert not response.cache_control.public
    assert first.headers["Content-Security-Policy"] != second.headers["Content-Security-Policy"]
