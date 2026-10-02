"""The header exposes enabled destinations directly and respects permissions."""
import re

import pytest


def navigation(html):
    return re.search(r'<nav class="nav".*?</nav>', html, re.S).group(0)


@pytest.mark.parametrize('language', ['en', 'it'])
def test_destinations_are_direct_links_in_the_header(admin, language):
    response = admin.get('/developer', headers={'Accept-Language': language})
    assert response.status_code == 200
    nav = navigation(response.get_data(as_text=True))
    assert 'href="/developer" aria-current="page"' in nav
    assert 'href="/chat"' in nav
    assert 'href="/personalities"' in nav
    assert '<details' not in nav
    assert '<summary' not in nav


def test_admin_destination_is_only_offered_to_administrators(client, admin, make_user):
    assert re.search(r'href="/admin/?"', navigation(admin.get('/account').get_data(as_text=True)))
    make_user('reader')
    client.logout()
    client.login('reader')
    nav = navigation(client.get('/account').get_data(as_text=True))
    assert 'href="/developer"' in nav
    assert not re.search(r'href="/admin/?"', nav)
