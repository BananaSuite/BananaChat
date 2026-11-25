# Sign in with BananaWiki Hosting

BananaChat can let people sign in with their
[BananaWiki Hosting](https://hosting.bananawiki.com) account. BananaChat is
an OAuth 2.0 client of the hosting portal (authorization code with PKCE),
following the BananaSuite sign-in contract. BananaChat keeps its own
accounts: a hosting account is *connected* to one BananaChat account, and
that account's role, limits, two-step verification and data stay here.

## Setting it up

1. On the portal, register an app (**Account → Developer apps**). As its
   redirect URI enter exactly:

   ```text
   https://<your BananaChat address>/login/hosting/callback
   ```

   **Admin → Settings → Sign-in** shows this address as BananaChat builds it
   (behind a reverse proxy, `BC_PROXY_MODE=1` makes it use the public name
   and `https`). The portal compares it character by character. Until a
   hosting administrator has reviewed the app, only its owner and hosting
   administrators can sign in with it.
2. Put the app's credentials in the server's environment (managed
   installations: `/opt/bananachat/config/app.env`) and restart:

   | Variable | Example | Purpose |
   | --- | --- | --- |
   | `BC_HOSTING_OAUTH_URL` | `https://hosting.bananawiki.com` | The portal. `/oauth/authorize`, `/oauth/token` and `/oauth/userinfo` are found under it. Must be `https://` (plain `http://` only to this machine, for a development portal). |
   | `BC_HOSTING_OAUTH_CLIENT_ID` | `bwa_…` | The app's client id. |
   | `BC_HOSTING_OAUTH_CLIENT_SECRET` | | The app's client secret (shown once by the portal). It is only sent to the portal's token endpoint (HTTP Basic) and never logged or shown. |

   All three are needed; a missing or invalid one leaves the feature off and
   logs a warning at start-up.
3. Choose the **sign-in mode** on **Admin → Settings → Sign-in**.

## Sign-in modes

| Mode | Sign-in page | Sign-up |
| --- | --- | --- |
| **Local accounts only** (`local`, the default) | Username and password, as before. The hosting pages answer 404. | As before. |
| **Both** (`both`) | The password form and **Sign in with BananaWiki Hosting**. | Both: the sign-up page, or the hosting button. |
| **BananaWiki Hosting accounts only** (`hosting`) | Only **Sign in with BananaWiki Hosting**; passwords are refused. | **Create an account** signs up at BananaWiki Hosting ([sign-up from an app](#sign-up-from-an-app)); `/signup` (and invitation links) lead to the hosting button. |

Without a complete configuration the mode in force is always `local`, whatever
is saved; the setting is shown disabled with the reason. Changing the mode asks
for a recent confirmation (your password or authenticator code), like other
high-impact settings, and is recorded in the audit log.

## The first hosting sign-in

The portal tells BananaChat the hosting account's stable id. BananaChat looks
it up among its connections only: **a hosting account is never matched to a
BananaChat account by user name or email**. The first time, the person
chooses:

- **Create a new account.** The hosting name is suggested, or the closest free
  name when it is taken or not allowed here (3–32 letters, digits, dots,
  hyphens and underscores). The site's sign-up setting applies: closed sign-up
  offers no new account, invitation-only sign-up asks for an invitation code
  (in hosting-only mode an invitation link carries its code along), and accounts made per address
  and hour count as on the sign-up page. The new account gets a random
  password nobody knows; it signs in through BananaWiki Hosting.
- **Connect an existing account** ("I already have an account here"), proven
  with its username and password, and its two-step code when it uses two-step
  verification. Wrong passwords count towards the same limits as the sign-in
  page. The page shows this choice first; a refused form keeps what was typed
  (passwords excepted).

A BananaChat account is connected to at most one hosting account, and a
hosting account to at most one BananaChat account.

Later sign-ins go straight to the connected account. A hosting sign-in never
asks for BananaChat's own two-step code: the portal asks for its own. Two-step
codes are asked after password sign-ins only (and the administrators'
requirement applies to accounts with a password). A suspended account is
refused as with a password.
Administrators' hosting sign-ins are in the audit log (`auth.admin_login`,
method `hosting`), other accounts' as `auth.hosting_login` (at most ten per
account in 15 minutes).

## Sign-up from an app

A5 of the BananaSuite sign-in contract: someone without an account creates a
BananaWiki Hosting account from this site's sign-in page and comes back
signed in. The account works on the portal, on every hosted wiki and here.

- In `hosting` mode, while the site takes new accounts (*Open* or
  *Invitation only*), the sign-in page shows **No account yet? Create an
  account**. It goes to `/login/hosting/start?create=1`, which asks the portal
  for its sign-up form first (`prompt=create`). In `both` mode the site's own
  sign-up page stays and nothing is added.
- The link is shown only while the portal's OAuth metadata
  (`/.well-known/oauth-authorization-server`) says `"signup": "open"` or
  `"invite"`. The answer is remembered for five minutes per process; a portal
  that does not answer within three seconds, or does not say, hides the link
  for a minute.
- The portal applies its own sign-up rules (invite codes, address, approval,
  limits). An account that must confirm its address or wait for approval sees
  the portal's page for that, with a link back here.
- The flow's state remembers it was a sign-up. Back here, an unknown hosting
  account gets its account at once, named like the hosting account, under
  this site's sign-up setting and with the sign-up page's limits per address:
  no choice page. When that is not possible (the name is taken or not allowed
  here, the site needs an invitation code, sign-up is closed) the first
  sign-in's page takes over, with the reason and the create form filled in.
- An acting hosting administrator never creates an account.

## Sign-in methods

**Account → Security → Sign-in methods** shows, in one place:

- **BananaWiki Hosting**: connected as *name* since *date*, or **Connect**
  (with the password; it then continues on the portal and is refused when that
  hosting account is connected to someone else). **Disconnect** needs a
  password of the account's own and a mode other than hosting-only; the page
  says why otherwise.
- **Password**: **Change**, or **Set password** for an account made through
  BananaWiki Hosting (it has no password of its own,
  `users.password_set = 0`), after confirming with BananaWiki Hosting.
- **Two-step verification**: set up or turn off, with the note that codes are
  asked after password sign-ins. An account without a password sets one first.

BananaChat accounts have no email address, so there is no email row.

Administrators see the connection on the account's page in **Admin → Users**
and can remove it there or on the linking page (audited as
`admin.user_hosting_unlink`; that account is signed out everywhere and
connects again, with its password, at its next hosting sign-in).

## Confirming with BananaWiki Hosting

Wherever BananaChat asks for the password before a sensitive change (the
password, two-step verification, disconnecting, deleting all chats or the
account, and the administrators' confirmed actions), a connected account can
choose **Confirm with BananaWiki Hosting** instead; an account without a
password of its own sees only this. BananaChat sends the person to the portal
with `prompt=login` and `max_age=0` (a fresh sign-in, the portal's two-step
step included), with a `state` bound to the account and the browser session
that started it, and accepts the answer only when:

- the portal account (`sub`) is the one connected to the account,
- the portal says when that sign-in happened (`auth_time`) and it is no older
  than the start of the confirmation, less 60 seconds, and
- no administrator is acting as the account (below).

It then counts like a password confirmation for ten minutes in that browser
session (and as the administrators' recent confirmation), and is audited as
`account.hosting_confirm`. A portal that does not send `auth_time` (releases
before the BananaSuite sign-in round 3) never confirms anything: the person
gets a message saying so and keeps using the password.

## Hosting administrators acting as users

When the portal lets one of its administrators act as an account
(`admin_app_signin` on the portal), the portal's answer carries `act` (the
administrator) and `act_expires_at`. BananaChat then:

- signs in only to an account already connected to that hosting account: it
  never connects or creates one while acting (an unknown account is refused
  with a message), and refuses everything when **Allow hosting administrators
  to sign in as users** (Admin → Settings → Sign-in, on by default: the
  portal's setting is the main switch) is off;
- shows "*administrator* is signed in as *user*" on every page, with the end
  time and a sign-out button, and ends the session at `act_expires_at` (at most
  12 hours);
- refuses the password, two-step verification, sign-in methods (connect,
  disconnect, confirm), deleting all chats or the account, API keys, and every
  administrator action that needs a confirmation;
- records the sign-in (`auth.hosting_acting_login`, refusals as
  `auth.hosting_acting_refused`) and every audited action of that session as
  "*administrator* (BananaWiki Hosting) on behalf of *user*", with the
  administrator's portal account id.

No two-step code is asked and no device cookie is issued for such a session.

## Linking accounts (administrators)

**Admin → Users → BananaWiki Hosting** lists every account with its link and
connects accounts in bulk. It always shows a preview first:

- Rules: one account; every account to the hosting account with the **same
  user name** (capitals don't matter); or a **pasted list** of
  `local_username,hosting_username` or `local_username,hosting_sub` lines (at
  most 500). BananaChat accounts have no email address, so there is no rule by
  email.
- Rows: *will link*, *already linked*, *conflict* (the hosting account is
  connected to another account here, the account here to another hosting
  account, several candidates, a suspended or pending hosting account), *not
  found*, or *skipped* (administrators unless **Include administrators** is
  ticked; suspended accounts).
- **Link** applies only the *will link* rows of that exact preview: BananaChat
  computes it again and compares a digest (an HMAC with the server's secret
  key); anything changed shows the new preview and links nothing. It needs a
  recent confirmation, and each link is audited (`admin.hosting_link`, with the
  rule). Each row has **Unlink**.
- **Switch to hosting-only sign-in** (also on the settings page) is refused
  while any active administrator is not connected.

Hosting user names are found with the portal's account lookup
(`/api/v1/admin/accounts/lookup`), which needs a portal API token of a portal
administrator with the `accounts:lookup` scope: **Admin → Settings → Sign-in →
Advanced**. The token is stored encrypted with a key derived from the server's
secret key, never shown again and never written to the audit log. Without a
token the page takes portal account ids only. BananaChat has no email
addresses, so no notice email is sent.

## Hosting-only mode and lock-outs

- **Guard.** Hosting-only sign-in can be chosen only while every active
  administrator is connected, the one switching included (choose *Both*,
  connect the accounts, then switch).
- **Recovery page.** While hosting-only is in force, administrators can still
  sign in with their password, and their two-step code, at
  `/login/recovery`. The page is not linked from the sign-in page, answers
  other accounts exactly like a wrong password, allows 10 attempts per address
  in 15 minutes on top of the sign-in limits, and every attempt is in the
  audit log (`auth.recovery_login`, `auth.recovery_login_failed`).
- **From the server.** As the service's user, with its environment:

  ```sh
  python -m bananachat.manage signin-mode           # show the saved and effective mode
  python -m bananachat.manage signin-mode both      # or local / hosting
  ```

  A change is recorded in the audit log (`operator.signin_mode`, by "server
  operator"). Like `reset-two-step`, it never creates or upgrades a database.
  Removing `BC_HOSTING_OAUTH_*` and restarting also brings back password
  sign-in (`local`).

Accounts created through BananaWiki Hosting have no password they know. In
`both` mode they can only sign in with BananaWiki Hosting until they set a
password (Account → Security, after confirming with BananaWiki Hosting); an
administrator can also give one a password (Admin → Users → Reset password).
Accounts made that way before schema version 33 are recognised from the audit
log.

## Security notes

- Each flow carries a random `state` kept in the session cookie (at most five
  at once, each for 10 minutes, used once) and a PKCE S256 challenge. The PKCE
  verifier is derived from the state with the server's secret key, so the
  cookie holds nothing secret.
- The callback is limited per address like sign-in requests
  (`BC_LOGIN_ATTEMPTS_PER_MINUTE`).
- The code is exchanged once on the token endpoint with HTTP Basic client
  authentication; the access token is sent to `/oauth/userinfo` in the
  `Authorization` header only, read once and dropped: it is never stored or
  logged. BananaChat asks only for the `profile` scope.
- Calls to the portal go to the configured origin only, without proxies or
  redirects, with 10-second timeouts and answers of at most 64 KiB.
- The account page, the two-step page and administrator pages asking for a
  confirmation name the portal in their `form-action` content security policy
  only while they offer to connect an account or to confirm with it.
- Account lookups go to the same origin, with answers of at most 1 MiB, in
  batches of 500 names.
