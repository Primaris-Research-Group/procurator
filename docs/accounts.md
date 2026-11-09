# Accounts: Apple and Google (web 0.29.0)

The **Accounts** tab of the dashboard (`/dashboard` in Telegram → Accounts) connects the owner's Apple and Google accounts with one email
address and one app password each. The broker checks each service against the real provider while the owner waits, keeps the password
only in `secrets/accounts/<id>.json` (mode 0600, never in chat, logs, the model or Telegram) and shows which services are live.

## What one password unlocks

| Provider | Password to make | Live from the box | Needs the Mac companion | Needs Google sign-in |
|---|---|---|---|---|
| Apple (iCloud) | App-specific password: account.apple.com › Sign-In and Security › App-Specific Passwords | Mail (IMAP), Calendar (CalDAV), Contacts (CardDAV); Send when the host can reach port 587 | Reminders, Notes, iMessage, Send when the host blocks SMTP | — |
| Google (Gmail) | App password: myaccount.google.com/apppasswords (2-Step Verification on; not for Workspace accounts) | Mail (IMAP), Send (SMTP) | — | Calendar, Contacts, Tasks, Drive files |

Why two routes for Google: reading Gmail through the API needs **restricted** scopes (`gmail.readonly`, `gmail.modify`) which require Google's
CASA security audit for any shared client. IMAP with an app password reads the same mailbox with no scope at all, so the shared PROCURATOR
OAuth client asks only for **sensitive** scopes (calendar, contacts.readonly, tasks, gmail.send, drive.file) — verification without an audit.

## Google sign-in (Settings › Connections › Google)

1. Operator once per box: `bash bin/set-oauth-client.sh google` with a *Web application* OAuth client whose redirect URI is
   `https://<domain>/assistant/dashboard/oauth/callback`. Publish the consent screen to **production** (in Testing mode refresh tokens
   expire after 7 days). Until Google verifies the client the consent screen shows an "unverified app" warning (Advanced › continue) and the
   client is capped at 100 users — fine for pilots.
2. Owner: Settings › Connections › Google › Connect, sign in, approve. Tokens live in `secrets/oauth/google/<grant>.json`.

## Tools the model gets

| Tool | Risk | What |
|---|---|---|
| `accounts_list` | read | Which accounts and services are connected (never a password) |
| `calendar_apple_events` | read | iCloud events for a range (default next 7 days), optionally one calendar |
| `contacts_apple_search` | read | iCloud contacts by name, phone digits or email (cached 10 min) |
| `calendar_apple_create_event` | own_write | One event in the owner's own iCloud calendar, no attendees |

Mail tools are unchanged (mail plugin); the mail broker reads the iCloud credentials from `/var/lib/procurator-mail/icloud.json` when the
operator wrote it, otherwise from the Accounts tab's copy `secrets/accounts/icloud-mail.json`.

## Behaviour to know

- A wrong password fails the first check and **saves nothing**; the message names the provider, never the value.
- Apple and Google revoke every app password when the account password changes: the account shows **reconnect** after *Check again*, and the
  owner reconnects on the same tab.
- Disconnect deletes the secret file(s) and the row.
- Calendar titles, locations and contact names reach the model wrapped as untrusted text (someone else may have written them).

## Files

`integrations/web/accounts.py` (store, probes, providers), `icloud_dav.py` (stdlib CalDAV/CardDAV), `accounts_tools.py` (model tools),
`connectors/core-accounts.json`, `integrations/web-plugin/accounts.mjs`, `bin/set-oauth-client.sh`, tests `test_accounts.py`,
`test_accounts_tools.py`, `accounts.test.mjs`.
