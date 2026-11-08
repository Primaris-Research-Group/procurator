# Public OAuth information pages

Published November 2, 2026 at https://assistant.example.com/assistant/ using the
owner-selected domain and existing DigitalOcean droplet. Source pages are in
`dist/`; the deployed files are copied to `site/assistant/` on the server.
These are static information pages, with no forms, analytics, cookies or scripts.
Update their disclosures when enabled assistant features or data handling change.

`assistant-public-site.service` runs a separate Nginx master using only this
directory's `nginx.conf`. It serves `/assistant/` and the HTTP ACME challenge path.
It does not include old Nginx application configuration or proxy to OpenClaw.
The legacy app and main `nginx.service` remain disabled. Dashboard publishing
remains on server loopback. The site starts automatically after reboot.

The existing Let's Encrypt certificate covers the root and www domain. Its old
Nginx-installer renewal setting was backed up under
`/root/personal-assistant-migration/renewal-before-static-site-*.conf` and removed.
`certbot reconfigure` then migrated to the new ACME webroot, tested both domains
against the staging service, and saved a deploy hook that reloads only
`assistant-public-site.service`. The pre-existing `certbot.timer` remains active.
The active certificate was not replaced by the staging test.

Checks passed: internal HTML links, homepage visual review, HTTPS 200 for all three
pages, outside-host homepage HTTPS access, and rejection of root, `.env` and a
traversal attempt. Public access logging is disabled; server errors use the journal.

Google Branding now contains the homepage, privacy and terms URLs and the
authorized domain `assistant.example.com`. The Audience page shows In production.
The app remains unverified for personal use. The owner completed fresh production
consent on November 2; the saved-grant receipt and live refresh check passed.
Never capture callback codes in screenshots or chat. For future reauthorization,
use the hidden-input helper:

```sh
python3 /opt/procurator/bin/connect-google.py --start --force-consent
```

After browser consent, copy the full localhost return address, return to the console,
and run `python3 /opt/procurator/bin/connect-google.py --finish`.
Paste it into that command's hidden prompt. Start exits before the browser steps,
so a console disconnect does not cancel the prepared sign-in. Completion is recorded
only after the new grant is saved and its connection check succeeds.

To update pages, copy `dist/` into `site/assistant/` on the server and validate
content and links. HTML changes do not require a reload. For config changes, run
`nginx -t -c /opt/procurator/oauth-public/nginx.conf`, then
`systemctl reload assistant-public-site.service`.

Stopping this service removes the public information pages; it does not stop the
Telegram assistant. Reversing the legacy migration requires separate explicit work.
