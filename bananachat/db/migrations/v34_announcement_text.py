"""Version 34: the announcement shows whenever it has a text.

Admin settings offered "Show an announcement on every page" next to the announcement's text. The switch
is gone: a saved text is shown. Where the switch was off, the stored text is emptied, so nothing appears
that did not before; ``site_settings.warning_banner_enabled`` follows the text for older readers.

Safe to run again. Frozen: do not import live defaults or validators.
"""


def upgrade(conn):
    conn.execute("UPDATE site_settings SET warning_banner_message='' WHERE warning_banner_enabled=0")
    conn.execute("UPDATE site_settings SET warning_banner_enabled="
                 "CASE WHEN TRIM(COALESCE(warning_banner_message, ''))='' THEN 0 ELSE 1 END")
