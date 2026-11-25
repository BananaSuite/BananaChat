"""BananaWiki Hosting links in the administration (B of the BananaSuite sign-in contract).

* ``/admin/users/<id>/hosting/unlink``: removes one account's link (audited) and signs the account out
  everywhere: its sessions may have come through that link. The person can connect again (with the
  account's password) on their next hosting sign-in. An administrator cannot remove their own link while
  only BananaWiki Hosting signs in.
* ``/admin/users/hosting``: every account with its link state, the linking tool and the switch to
  hosting-only sign-in (refused while an active administrator is not linked, see
  ``settings.hosting_only_problem``).

The linking tool always shows a preview first. Its rules: one account (a local user name and a BananaWiki
Hosting account), the same user name (case-insensitive), or a pasted list of
``local_username,hosting_username`` or ``local_username,hosting_sub`` lines. BananaWiki Hosting user names
are found with the portal's account lookup (``hosting_signin.lookup``, which needs the portal API token
stored in Settings → Sign-in); without a token only portal account ids (``sub``) are accepted. BananaChat
accounts have no email address, so there is no rule by email. Each preview row will link, is already linked,
is a conflict (the hosting account is linked to another account here, the account here is linked to another
hosting account, several candidates, a suspended or pending hosting account), was not found, or is skipped
(administrators unless included, suspended accounts here). Apply recomputes the preview and links the rows
that will link only when they are exactly the ones shown (a digest, an HMAC with the server's secret key);
otherwise it shows the new preview. Apply needs a recent confirmation; every link and unlink is audited with
its rule. Nothing here works while a hosting administrator acts as an administrator here (_reauth).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from flask import current_app, flash, render_template, request

from bananachat import db, security
from bananachat.db import hosting_links, users
from bananachat.db import settings as site_settings
from bananachat.security import admin_required
from bananachat.services import hosting_signin

from . import _reauth, bp
from ._helpers import FormError, audit, back, fail, flag, me, page, user_or_404
from .settings import hosting_only_problem

RULES = ("one", "username", "list")
RULE_LABELS = {"one": "one account", "username": "same user name", "list": "pasted list"}
COLUMNS = ("username", "sub")
LIST_MAX_LINES = 500
LIST_MAX_CHARS = 64 * 1024
# The "same user name" rule looks every account up; past this many, a pasted list is the way.
PREVIEW_MAX_USERS = 5000
PAGE_SIZE = 50
# Previews and applies per administrator and minute: each asks the portal (which allows 60 lookups a minute).
LINK_REQUESTS_PER_MINUTE = 20
STATES = ("", "linked", "unlinked")
STATUS_ORDER = ("will_link", "already_linked", "conflict", "not_found", "skipped")
STATUS_LABELS = {"will_link": "Will link", "already_linked": "Already linked", "conflict": "Conflict",
                 "not_found": "Not found", "skipped": "Skipped"}
STATUS_BADGES = {"will_link": "badge-success", "already_linked": "", "conflict": "badge-danger",
                 "not_found": "badge-warning", "skipped": ""}


@dataclass
class Row:
    """One line of a preview: the account here, what it was matched with, and what Apply would do."""

    local: str
    user_id: str | None
    target: str
    role: str = ""
    sub: str = ""
    portal_username: str = ""
    status: str = ""
    reason: str = ""


# ----- removing one link ----------------------------------------------------------------

@bp.post("/users/<user_id>/hosting/unlink", endpoint="user_hosting_unlink")
@admin_required
def unlink(user_id):
    user = user_or_404(user_id)
    from_tool = request.form.get("return_to") == "links"
    back_to = ("admin.hosting_links", {}) if from_tool else ("admin.user_detail", {"user_id": user_id})
    if user_id == me()["id"] and hosting_signin.effective_mode() == "hosting":
        return fail("Only BananaWiki Hosting accounts can sign in: removing your own link would lock you out. "
                    "Choose another sign-in mode first.", back_to[0], **back_to[1])
    removed = hosting_links.remove(user_id)
    if removed is None:
        flash(f"{user['username']} is not connected to BananaWiki Hosting.", "info")
        return back(back_to[0], **back_to[1])
    if user_id != me()["id"]:
        users.revoke_sessions(user_id)
    audit("user_hosting_unlink", user["username"], {"portal_username": removed["portal_username"],
                                                    "sub": removed["account_id"]})
    flash(f"{user['username']} is no longer connected to BananaWiki Hosting"
          + ("." if user_id == me()["id"] else " and was signed out everywhere."), "success")
    return back(back_to[0], **back_to[1])


# ----- the page -----------------------------------------------------------------------------

def _page(*, rows: list[Row] | None = None, inputs: dict | None = None, digest: str = "", status: int = 200):
    search = (request.args.get("q") or "").strip()[:64]
    state = request.args.get("state") if request.args.get("state") in STATES else ""
    current = page(hosting_links.users_count(search, state), PAGE_SIZE)
    counts = {key: 0 for key in STATUS_ORDER}
    for row in rows or []:
        counts[row.status] += 1
    return render_template(
        "admin/hosting_links.html", section="users", configured=hosting_signin.configured(),
        mode=hosting_signin.stored_mode(), effective=hosting_signin.effective_mode(),
        lookup=hosting_signin.lookup_state(), summary=hosting_links.counts(),
        accounts=hosting_links.users_page(PAGE_SIZE, current.offset, search, state), page=current, search=search,
        state=state, rows=rows, counts=counts, inputs=inputs or {"rule": "one", "column": _default_column()},
        digest=digest, labels=STATUS_LABELS, badges=STATUS_BADGES, rule_labels=RULE_LABELS,
        hosting_only=hosting_only_problem(), max_lines=LIST_MAX_LINES, me_id=me()["id"],
    ), status


def _default_column() -> str:
    return "username" if hosting_signin.lookup_state() == "ready" else "sub"


@bp.get("/users/hosting", endpoint="hosting_links")
@admin_required
def links_page():
    return _page()


# ----- the preview --------------------------------------------------------------------------

def _inputs() -> dict:
    """The linking tool's form, checked (FormError says what is wrong)."""
    rule = request.form.get("rule")
    if rule not in RULES:
        raise FormError("Choose how to match the accounts.")
    column = request.form.get("column") or "username"
    if column not in COLUMNS:
        raise FormError("Choose whether the list names BananaWiki Hosting user names or portal account ids.")
    if rule == "one":
        local = (request.form.get("local_username") or "").strip()[:64]
        target = (request.form.get("hosting_account") or "").strip()[:200]
        if not local or not target:
            raise FormError("Enter the user name here and the BananaWiki Hosting account.")
        lines = f"{local},{target}"
    elif rule == "list":
        lines = request.form.get("lines") or ""
        if len(lines) > LIST_MAX_CHARS:
            raise FormError(f"The list can be at most {LIST_MAX_CHARS // 1024} KB.")
        # As the preview's hidden copy comes back (browsers change line ends and drop a first empty line).
        lines = "\n".join(line.strip() for line in lines.splitlines() if line.strip())
    else:
        lines, column = "", "username"
    return {"rule": rule, "column": column, "lines": lines, "include_admins": flag("include_admins"),
            "local_username": (request.form.get("local_username") or "").strip()[:64] if rule == "one" else "",
            "hosting_account": (request.form.get("hosting_account") or "").strip()[:200] if rule == "one" else ""}


def _pairs(text: str) -> list[tuple[str, str]]:
    """``(local user name, hosting account)`` for each line of a pasted list (a header line is skipped)."""
    pairs = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or (number == 1 and line.lower().startswith("local_username")):
            continue
        parts = [part.strip() for part in re.split(r"[,;\t]", line, maxsplit=1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise FormError(f"Line {number}: write the user name here, a comma, then the BananaWiki Hosting "
                            "account.")
        pairs.append((parts[0][:64], parts[1][:200]))
        if len(pairs) > LIST_MAX_LINES:
            raise FormError(f"Paste at most {LIST_MAX_LINES} lines at a time.")
    if not pairs:
        raise FormError("Paste at least one line: user name here, BananaWiki Hosting account.")
    return pairs


def _candidates(inputs: dict) -> list[tuple[str, str, object]]:
    """``(name as given, hosting account as given, the account here or None)`` for the rule."""
    if inputs["rule"] == "username":
        accounts = db.query("SELECT * FROM users ORDER BY lower(username), id LIMIT ?", (PREVIEW_MAX_USERS + 1,))
        if len(accounts) > PREVIEW_MAX_USERS:
            raise FormError(f"There are more than {PREVIEW_MAX_USERS} accounts: link them with pasted lists.")
        return [(row["username"], row["username"], row) for row in accounts]
    return [(local, target, users.get_by_username(local)) for local, target in _pairs(inputs["lines"])]


def compute(inputs: dict) -> list[Row]:
    """The preview: every candidate with its status and the reason (raises FormError or hosting_signin.LookupFailed)."""
    candidates = _candidates(inputs)
    if inputs["column"] == "username" and hosting_signin.lookup_state() != "ready":
        raise FormError("BananaWiki Hosting user names need the portal API token (Settings → Sign-in, Advanced). "
                        "Without it, enter portal account ids.")
    listed = Counter(user["id"] for _, _, user in candidates if user is not None)
    rows, wanted = [], set()
    for local, target, user in candidates:
        row = Row(local=user["username"] if user else local, user_id=user["id"] if user else None, target=target,
                  role=user["role"] if user else "")
        if user is None:
            row.status, row.reason = "not_found", "No account here has this user name."
        elif users.is_suspended(user):
            row.status, row.reason = "skipped", "The account here is suspended."
        elif user["role"] == "admin" and not inputs["include_admins"]:
            row.status, row.reason = "skipped", "Administrator: tick Include administrators to link it."
        elif listed[user["id"]] > 1:
            row.status, row.reason = "conflict", "This account is listed more than once."
        elif inputs["column"] == "username":
            wanted.add(target)
        rows.append(row)
    found = hosting_signin.lookup(sorted(wanted)) if wanted else {}
    links = {row["user_id"]: row for row in db.query("SELECT * FROM hosting_links")}
    by_sub = {row["account_id"]: row["user_id"] for row in links.values()}
    names = {}
    for row in rows:
        if row.status:
            continue
        if inputs["column"] == "username":
            matches = found.get(row.target.lower(), [])
            if not matches:
                row.status, row.reason = "not_found", "No BananaWiki Hosting account has this user name."
                continue
            if len({match.sub for match in matches}) > 1:
                row.status, row.reason = "conflict", "Several BananaWiki Hosting accounts match."
                continue
            account = matches[0]
            row.sub, row.portal_username = account.sub, account.username
            if account.status != "active":
                row.status, row.reason = "conflict", f"The BananaWiki Hosting account is {account.status}."
                continue
        else:
            if not hosting_signin._valid_sub(row.target):
                row.status, row.reason = "not_found", "This is not a portal account id."
                continue
            row.sub = row.target
        current = links.get(row.user_id)
        if current is not None:
            if current["account_id"] == row.sub:
                row.status, row.reason = "already_linked", ""
            else:
                row.status = "conflict"
                row.reason = ("The account here is linked to another BananaWiki Hosting account ("
                              f"{current['portal_username'] or current['account_id']}).")
            continue
        owner = by_sub.get(row.sub)
        if owner is not None:
            if owner not in names:
                holder = users.get(owner)
                names[owner] = holder["username"] if holder else owner
            row.status, row.reason = "conflict", f"This BananaWiki Hosting account is linked to {names[owner]}."
            continue
        row.status = "will_link"
    targets = Counter(row.sub for row in rows if row.status == "will_link")
    for row in rows:
        if row.status == "will_link" and targets[row.sub] > 1:
            row.status, row.reason = "conflict", "Several accounts here would link to this BananaWiki Hosting account."
    rows.sort(key=lambda row: (STATUS_ORDER.index(row.status), row.local.lower()))
    return rows


def digest(inputs: dict, rows: list[Row]) -> str:
    """What Apply checks: the rule and the exact rows that would be linked."""
    payload = json.dumps({"rule": inputs["rule"], "column": inputs["column"], "lines": inputs["lines"],
                          "include_admins": inputs["include_admins"],
                          "link": [[row.user_id, row.sub, row.portal_username] for row in rows
                                   if row.status == "will_link"]}, sort_keys=True, ensure_ascii=False)
    key = current_app.config["BC"].secret_key.encode("utf-8")
    return hmac.new(key, b"hosting-link-preview:" + payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _preview(inputs: dict, status: int = 200):
    rows = compute(inputs)
    return _page(rows=rows, inputs=inputs, digest=digest(inputs, rows), status=status)


@bp.post("/users/hosting/preview", endpoint="hosting_link_preview")
@admin_required
@security.rate_limit("admin-hosting-links", LINK_REQUESTS_PER_MINUTE, 60, per_user=True)
def preview():
    if not hosting_signin.configured():
        return fail("Sign-in with BananaWiki Hosting is not configured.", "admin.hosting_links")
    inputs = None
    try:
        inputs = _inputs()
        return _preview(inputs)
    except (FormError, hosting_signin.LookupFailed) as error:
        flash(str(error), "error")
        draft = inputs or {"rule": request.form.get("rule") or "one",
                                           "column": request.form.get("column") or _default_column(),
                                           "lines": (request.form.get("lines") or "")[:LIST_MAX_CHARS],
                                           "include_admins": flag("include_admins"),
                                           "local_username": (request.form.get("local_username") or "")[:64],
                                           "hosting_account": (request.form.get("hosting_account") or "")[:200]}
        return _page(inputs=draft, status=400)


# ----- applying it -----------------------------------------------------------------------------

class _Changed(Exception):
    pass


@bp.post("/users/hosting/apply", endpoint="hosting_link_apply")
@admin_required
@security.rate_limit("admin-hosting-links", LINK_REQUESTS_PER_MINUTE, 60, per_user=True)
def apply():
    if not hosting_signin.configured():
        return fail("Sign-in with BananaWiki Hosting is not configured.", "admin.hosting_links")
    try:
        inputs = _inputs()
        if not _reauth.confirmed():
            return _preview(inputs, 403)
        rows = compute(inputs)
    except (FormError, hosting_signin.LookupFailed) as error:
        flash(str(error), "error")
        return back("admin.hosting_links")
    expected = digest(inputs, rows)
    sent = request.form.get("digest") or ""
    if not (sent.isascii() and hmac.compare_digest(sent, expected)):
        flash("Something changed since the preview, so nothing was linked. Check this new preview and apply it "
              "again.", "error")
        return _page(rows=rows, inputs=inputs, digest=expected, status=409)
    chosen = [row for row in rows if row.status == "will_link"]
    if not chosen:
        flash("Nothing to link.", "info")
        return back("admin.hosting_links")
    try:
        with db.transaction():
            for row in chosen:
                user = users.get(row.user_id)
                if (user is None or users.is_suspended(user) or hosting_links.link_of(row.user_id) is not None
                        or hosting_links.linked_user(row.sub) is not None):
                    raise _Changed
                hosting_links.create(row.user_id, row.sub, row.portal_username)
    except (_Changed, sqlite3.IntegrityError):
        flash("Something changed while linking, so nothing was linked. Check this new preview and apply it "
              "again.", "error")
        rows = compute(inputs)
        return _page(rows=rows, inputs=inputs, digest=digest(inputs, rows), status=409)
    for row in chosen:
        audit("hosting_link", row.local, {"rule": RULE_LABELS[inputs["rule"]], "sub": row.sub,
                                          "portal_username": row.portal_username})
    count = len(chosen)
    flash(f"Linked {count} account{'s' if count != 1 else ''} to BananaWiki Hosting.", "success")
    return back("admin.hosting_links")


# ----- hosting-only sign-in ----------------------------------------------------------------------

@bp.post("/users/hosting/hosting-only", endpoint="hosting_only")
@admin_required
def hosting_only():
    """Only BananaWiki Hosting signs in from now on: refused while an active administrator is not linked."""
    if not hosting_signin.configured():
        return fail("Sign-in with BananaWiki Hosting is not configured.", "admin.hosting_links")
    before = hosting_signin.stored_mode()
    if before == "hosting":
        flash("Only BananaWiki Hosting accounts sign in already.", "info")
        return back("admin.hosting_links")
    problem = hosting_only_problem()
    if problem:
        return fail(problem, "admin.hosting_links")
    if not _reauth.confirmed():
        return back("admin.hosting_links")
    site_settings.update(signin_mode="hosting")
    audit("hosting_only", "site", {"from": before, "to": "hosting"})
    flash("Only BananaWiki Hosting accounts sign in now. Administrators still have the recovery page.", "success")
    return back("admin.hosting_links")
