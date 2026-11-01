"""Version 29: effort and rate requests leave the community board by default.

Supporters renounce tokens only for token increases: an effort unlock or a
rate boost costs them nothing, so a few accounts could lift an
administrator's controls for one another. Sites still on the original
default (every kind) keep only the token kinds; an administrator can offer
effort and rate requests again on **Administration → Quotas**. A site that
chose its kinds itself keeps them.
"""

OLD_DEFAULT = "window,weekly,rate,temporary,effort"
NEW_DEFAULT = "window,weekly,temporary"


def upgrade(conn):
    conn.execute("UPDATE site_settings SET community_kinds=? WHERE community_kinds=?", (NEW_DEFAULT, OLD_DEFAULT))
