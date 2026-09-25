"""Text formatting helpers for templates."""


def plural(count, singular, plural_form=None):
    """Return "<count> <word>" with the word agreeing with the count.

    Pass plural_form for words that do not simply take a trailing "s".
    """
    word = singular if count == 1 else (plural_form or f"{singular}s")
    return f"{count} {word}"
