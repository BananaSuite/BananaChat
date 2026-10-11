# Two-step verification and recent confirmation

## For everyone

Any account can turn on two-step verification on **Account → Two-step
verification**. After the password, signing in then also asks for a six-digit
code from an authenticator app (any app for time-based codes, also called
TOTP: for example Aegis, Google Authenticator, Microsoft Authenticator or a
password manager). Someone who learns the password still cannot sign in.

- **Turning it on** shows a QR code and the same key as text, and asks for a
  code from the app and the password. It then shows ten **recovery codes**,
  once: each signs in one time when the phone is lost. Turning it on signs out
  the account's other sessions and, unless *Also revoke my API keys* is
  unticked (it is ticked by default), revokes the account's API keys.
- **New recovery codes** and **turning it off** ask for the password and a code
  from the app or a recovery code. New codes replace all the old ones.
- **API keys do not ask for a code**: scripts keep using them. Someone who
  briefly held a session may have created one, so every action that secures an
  account (turning this on, changing the password, signing out other devices,
  and an administrator's reset or *Sign out everywhere*) offers to revoke the
  account's keys, ticked by default. Untick it to keep the scripts working, or
  create new keys afterwards.
- Each app code works once. Wrong codes are counted per account and browser:
  after 10 wrong codes in 5 minutes, or 50 in a day, that browser waits. A
  browser that signed in to the account before has an allowance of its own,
  so someone who knows the password cannot lock its owner out.

## For administrators

**Require it for administrators** (Admin → Site settings → Sign-in security,
off by default) sends every administrator without it to set it up before the
administration pages open. Turn it on for your own account first; the page
shows how many administrators still have to.

When someone lost both their authenticator app and their recovery codes, an
administrator can reset it on the account's page (Admin → Users). The reset is
recorded in the audit log, ends every session of that account and, unless the
box is unticked, revokes its API keys; the person then signs in with the
password alone and can set it up again. Administrators turn off their own on
their account page.

### Recent confirmation

Someone who gets hold of an administrator's browser session (a stolen cookie,
an unlocked computer) can do whatever that administrator can while the session
lasts. The actions that would let them keep access after it ends, lock the
other administrators out, copy everyone's data or send users' prompts
somewhere new ask the administrator to confirm it is them, with their password,
or with a code from their authenticator app when they use two-step
verification:

- downloading the database export (Admin → Backup & migration);
- resetting another account's password or two-step verification;
- changing a role, creating an administrator account or an administrator
  invitation;
- suspending, reinstating or deleting an administrator;
- registering a worker (its token keeps working, and the worker reads the
  prompts sent to it);
- adding, changing or removing an external provider, with or without an API
  key (its connection decides where prompts and the key go; removing it
  deletes the key);
- turning the administrator requirement on or off, and opening sign-up to
  everyone.

The rest (moderation, limits, the model catalogue and downloads, provider tests
and model choices, site appearance) is recorded in the audit log but not
confirmed: it is reversible, or within what the session can already see and
remove. Every administrator action is classified in
`bananachat/web/admin/_reauth.py`, and a test fails when a new one is not.

Signing in counts as a confirmation, and a confirmation lasts ten minutes in
that browser; the forms show the extra field only when it is needed.
Confirmations and failed attempts are recorded in the audit log
(`admin.reauth`, `admin.reauth_failed`); after 10 wrong attempts in 15 minutes
the account waits.

## Storage and moving servers

Authenticator secrets are stored encrypted with a key derived from the
server's secret key (`BC_SECRET_KEY` or the instance's `.secret_key`), and
recovery codes only as SHA-256 digests (each code carries 80 random bits).
Keep the secret key with the instance directory in backups. A database export
moved to a server with another secret key keeps the recovery codes working
but not the app codes: those accounts sign in with a recovery code, turn
two-step verification off and set it up again.

If every administrator is locked out (no app, no recovery codes), or nobody
else can reset an account, turn it off from the server:

```sh
sudo bananachat reset-two-step USERNAME
```

It works on the server that keeps the accounts (single or web), as the
service's user and with its configuration, like the reset in Admin → Users:
it ends every session of that account, revokes its API keys, and the audit log
records `operator.user_two_step_reset` by "server operator". It prints
`"outcome": "reset"` with the number of keys revoked (`"api_keys_revoked"`), or
`"unchanged"` when that account does not use two-step verification; an unknown
name is refused. The person then signs in with the
password alone and can set it up again.

Without the managed installer, run the same command from the release
directory as the user the service runs as, with the service's environment
(at least `BC_DATABASE_PATH`, or `BC_INSTANCE_DIR`):

```sh
python -m bananachat.manage reset-two-step USERNAME
```

It never creates or upgrades a database: after an update, start the service
once before using it.
