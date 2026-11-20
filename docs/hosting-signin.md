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
| **BananaWiki Hosting accounts only** (`hosting`) | Only **Sign in with BananaWiki Hosting**; passwords are refused. | Through the hosting button: `/signup` (and invitation links) lead there. |

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
- **Connect an existing account**, proven with its username and password, and
  its two-step code when it uses two-step verification. Wrong passwords count
  towards the same limits as the sign-in page.

A BananaChat account is connected to at most one hosting account, and a
hosting account to at most one BananaChat account.

Later sign-ins go straight to the connected account. An account with two-step
verification still answers its code after the portal (the portal's own sign-in
does not replace it). A suspended account is refused as with a password.
Administrators' hosting sign-ins are in the audit log (`auth.admin_login`,
method `hosting`), other accounts' as `auth.hosting_login` (at most ten per
account in 15 minutes).

People can connect or disconnect their account on **Account → Security →
BananaWiki Hosting** with their password (connecting then continues on the
portal). Disconnecting is not possible in hosting-only mode. Administrators see
the connection on the account's page in **Admin → Users** and can remove it
there (audited as `admin.user_hosting_unlink`; that account is signed out
everywhere and connects again, with its password, at its next hosting sign-in).

## Hosting-only mode and lock-outs

- **Guard.** An administrator can switch to hosting-only only after connecting
  their own account (choose *Both*, connect it on the account page, then
  switch).
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
`both` mode they can only sign in with BananaWiki Hosting; an administrator can
give one a password (Admin → Users → Reset password) when it needs one, for
example to set up two-step verification.

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
- The account page names the portal in its `form-action` content security
  policy only while it offers to connect an account.
