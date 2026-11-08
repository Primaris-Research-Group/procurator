# Install PROCURATOR

About 20 minutes, most of it waiting. You need a credit card, a phone with Telegram, and a computer with a browser.

## 1. Rent a small server (5 minutes)

Any provider that gives you an **Ubuntu 24.04** server with a public address works. On DigitalOcean: **Create › Droplets › Ubuntu 24.04 › Basic › Regular, 4 GB** ($24/month; choose 8 GB for PROCURATOR-X) › set a root password › **Create Droplet**. Copy its IP address.

You do **not** need your own domain: the installer gives the server a free HTTPS address (`<your-ip>.sslip.io`). If you have one, point an A record at the server and pass `--domain`.

## 2. Run the installer (10 minutes, one line)

Open the server's console (DigitalOcean: your droplet › **Console**; or `ssh root@YOUR_IP` from a Mac Terminal) and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/Primaris-Research-Group/procurator/main/bin/install.sh | sudo bash -s -- --repo https://github.com/Primaris-Research-Group/procurator.git
```

Add `--edition x` for **PROCURATOR-X** (adds the coding orchestrator) and `--domain box.yourdomain.com` if you have your own domain.

It installs everything, turns on HTTPS, and ends by printing a **one-time setup link**:

```
  PROCURATOR is installed. One step left — open this link in your browser:

      https://203-0-113-10.sslip.io/assistant/dashboard/setup/…
```

## 3. Open the setup link (5 minutes, in your browser)

The page has three steps:

1. **Your AI model key.** It links to console.anthropic.com › API keys. Make a key, paste it, and the page checks it.
2. **Your Telegram bot.** The page walks you through @BotFather on your phone: `/newbot`, a name, then a username ending in `bot`. Paste the token BotFather sends, and the page shows your bot's name.
3. **Connect your phone.** Tap **Open my bot in Telegram** on your phone and press **Start**. The page now shows the assistant restarting, and the bot messages you: *"PROCURATOR is set up and you are its owner."*

Then say hi to your bot. It asks a few short questions, one at a time: what to call you, how to refer to you (he/him, she/her or they/them), your city, what you do, what you want help with first, and your main email. Say "skip" to pass on any of them. Send `/welcome` any time to redo it.

You never type a pairing code or look up a user id. Whoever presses Start with that one-time link becomes the only person the assistant listens to. The Telegram channel is then locked to an allow-list containing just you.

**Prefer the terminal?** When the installer asks, type `T` and press Enter, or run `sudo python3 /opt/procurator/bin/connect-accounts.py`. You get the same three steps with hidden input. The link it prints for step 3 also goes to your phone.

## 4. Connect Apple and Google (2 minutes each)

The setup page's last button opens your dashboard on the **Accounts** tab. Each provider card has numbered steps and a button to the right Apple or Google page:

- **iCloud:** your Apple Account email plus an **app-specific password** (account.apple.com › Sign-In and Security › App-Specific Passwords). Mail, Calendar and Contacts light up when each one works.
- **Gmail:** your Gmail address plus an **app password** (myaccount.google.com/apppasswords; 2-Step Verification must be on). Work and school Google accounts cannot make app passwords.
- **Google Calendar, Contacts and Tasks:** Settings › Connections › Google › Connect.

The dashboard's **Setup** tab shows what's done and the one next step for anything missing.

## Optional

| Add | How |
|---|---|
| Phone calls | `sudo python3 bin/connect-twilio.py` |
| Cloud browser | `sudo python3 bin/connect-browserbase.py` |
| Home-PC browser lane, PROCURATOR-X builders | `bin/setup-builder.sh` prints a one-line installer for your Mac or PC |
| Health and other experimental modules | off by default; see `docs/modules.md` |

## If something goes wrong

- **The link says it expired or was used:** `sudo bash /opt/procurator/bin/install.sh --setup-link` prints a fresh one.
- **The page doesn't load:** wait a minute (HTTPS certificates are issued on the first visit). If you used your own domain, check that its A record points at the server.
- **New phone or new bot:** `sudo bash /opt/procurator/bin/install.sh --setup-link --reconfigure`.
- **Health check:** `cd /opt/procurator && ./bin/assistant status`, `journalctl -u procurator-web -n 50`.
- **Upgrade:** run the installer again. Nothing in `.env`, `config/` or `state/` is replaced.
