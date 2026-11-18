# Deploy on AWS Lightsail (Ubuntu 24.04 LTS)

Runs the Hub (`main.py`) and the Discord bot as systemd services under a non-root `pmbot` user, with daily Vault backups.

## 1. Create the instance

1. In the Lightsail console, Create instance: Linux/Unix, OS only, Ubuntu 24.04 LTS, plan with 2 GB RAM.
2. Networking tab, Create static IP, attach it to the instance (keeps the IP across restarts).
3. Networking tab, IPv4 Firewall: keep only SSH (TCP 22). Remove HTTP (80) and any other rule. The bot and Hub make outbound connections only. Optionally restrict SSH to your own IP.
4. SSH in: `ssh ubuntu@<static-ip>` (or use the browser SSH).

## 2. Install

```bash
git clone https://github.com/shachard46/polymarket-trading-bot.git ~/pmbot-src && cd ~/pmbot-src
sudo PMBOT_REPO_URL=https://github.com/shachard46/polymarket-trading-bot.git bash deploy/setup.sh
```

Or, to run directly from this checkout: `sudo bash deploy/setup.sh "$PWD"` (the checkout becomes owned by `pmbot`, so keep it somewhere like /opt, not in a home directory).

`setup.sh` is idempotent. It installs python3-venv and git, creates the `pmbot` user, clones to /opt/pmbot, builds /opt/pmbot/.venv, installs requirements, creates /etc/pmbot/pmbot.env (mode 600) if missing, installs the systemd units and timer, enables ufw (OpenSSH only) and unattended-upgrades.

## 3. Configure

```bash
sudoedit /etc/pmbot/pmbot.env
```

Leave `PMBOT_MODE=stub` for now. Fill in the provider keys you need (`ANTHROPIC_API_KEY` by default) and the `DISCORD_*` values. For the Discord application, bot token, intents and channel IDs, see docs/06_concierge.md section 5.

## 4. Enable

```bash
sudo systemctl enable --now pmbot-hub pmbot-bot pmbot-backup.timer
systemctl status pmbot-hub pmbot-bot
systemctl list-timers pmbot-backup.timer
```

If you do not use Discord yet, enable only `pmbot-hub` and `pmbot-backup.timer`.

## 5. Verify

1. Stub tick (no cost): with `PMBOT_MODE=stub`, watch `journalctl -u pmbot-hub -f` until a pipeline tick finishes without errors, and check the new files under /opt/pmbot/Vault.
2. Live tick: set `PMBOT_MODE=live` and the provider keys, then `sudo systemctl restart pmbot-hub` and watch the log for the first tick. Live ticks call paid models: lower the `PMBOT_*_BUDGET` values first if you want a cheap first run.

## 6. Operate

- Logs: `journalctl -u pmbot-hub -f`, `journalctl -u pmbot-bot -f`, `journalctl -u pmbot-backup`.
- Backups: daily 03:30 (server time) to /var/backups/pmbot/vault-<date>.tar.gz, newest 14 kept. Set `PMBOT_BACKUP_S3_URI` (and install the aws cli) to also upload. Run now: `sudo systemctl start pmbot-backup`.
- Update:
  ```bash
  sudo -u pmbot git -C /opt/pmbot pull
  sudo -u pmbot /opt/pmbot/.venv/bin/pip install -r /opt/pmbot/requirements.txt
  sudo systemctl restart pmbot-hub pmbot-bot
  ```
  Re-run `setup.sh` after changes under deploy/ so the unit files are refreshed.
- Restore: stop the services, `tar -xzf <archive> -C /opt/pmbot`, fix ownership with `chown -R pmbot:pmbot /opt/pmbot/Vault`, start the services.
