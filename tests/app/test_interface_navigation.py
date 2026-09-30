"""The compact header keeps secondary destinations reachable and permission aware."""
import re

import pytest


def navigation(html):
    return re.search(r'<nav class="nav".*?</nav>', html, re.S).group(0)


@pytest.mark.parametrize('language,label', [('en', 'More'), ('it', 'Altro')])
def test_secondary_destination_is_identified_in_the_header(admin, language, label):
    response = admin.get('/developer', headers={'Accept-Language': language})
    assert response.status_code == 200
    nav = navigation(response.get_data(as_text=True))
    assert f'<summary class="is-current">{label}' in nav
    assert 'href="/developer" aria-current="page"' in nav
    # Native details makes secondary links available without a custom menu widget.
    assert '<details class="menu nav-more" data-menu>' in nav


def test_admin_destination_is_only_offered_to_administrators(client, admin, make_user):
    assert re.search(r'href="/admin/?"', navigation(admin.get('/account').get_data(as_text=True)))
    make_user('reader')
    client.logout()
    client.login('reader')
    nav = navigation(client.get('/account').get_data(as_text=True))
    assert 'href="/developer"' in nav
    assert not re.search(r'href="/admin/?"', nav)
    assert 'class="is-current"' not in nav
