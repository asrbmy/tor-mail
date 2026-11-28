# Automatic updates from GitHub

`scripts/update.sh` keeps a server running the latest **signed release** of `https://github.com/asrbmy/tor-mail`.
It runs on the host (never in a container), from a systemd timer or by hand.

## Read this first: what you are trusting

An update changes the code on your server **and the JavaScript your users' browsers run to encrypt their messages**.
Whoever can publish an update can read every future message. So updates are accepted only when they carry a signature made
by a key you chose, and the GitHub account alone is not enough:

| Attacker controls | Can they deploy to your server? |
|---|---|
| The GitHub account or repository | **No**: no valid signature, the update is refused |
| A force-push or older release | **No**: only fast-forwards of what is installed are accepted |
| Your release signing key | **Yes**, so keep it offline, on a hardware key if you can |
| Your server | Yes, but then everything is lost anyway |

Never set `REQUIRE_SIGNED=0` on a real server.

## One-time setup

**1. Publish the code in `asrbmy/tor-mail`** (the history there must contain what the server runs; the updater only moves forward).

**2. Make a release signing key** on your own computer (not the server):
```bash
gpg --quick-generate-key "Tor-Mail releases <you@example.org>" ed25519 sign 2y
gpg --fingerprint "Tor-Mail releases"          # write this fingerprint down; publish it somewhere you trust
gpg --armor --export "Tor-Mail releases" > release-key.pub
```

**3. Give the server only the public key**, and check the fingerprint by eye:
```bash
sudo install -d -m 700 /etc/tormail /etc/tormail/gnupg
sudo gpg --homedir /etc/tormail/gnupg --import release-key.pub
sudo gpg --homedir /etc/tormail/gnupg --fingerprint
```
This keyring must contain **only** keys allowed to publish releases.

**4. Install the updater on the server**
```bash
sudo git clone https://github.com/asrbmy/tor-mail.git /opt/tor-mail
sudo cp /opt/tor-mail/deployment/update.env.example /etc/tormail/update.env
sudo chmod 600 /etc/tormail/update.env       # edit it: AGE_RECIPIENT (backups), CHANNEL, HEALTH_TIMEOUT
sudo apt-get install -y age                  # Fedora: sudo dnf install -y age
```
The server's checkout must be a normal clone with no local edits (configuration lives in `.env`, which git ignores).

**5. Try it without changing anything**
```bash
sudo /opt/tor-mail/scripts/update.sh --check     # exit 10 = verified update available, 0 = up to date
```

**6. Switch on the timer**
```bash
sudo cp /opt/tor-mail/deployment/tormail-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tormail-update.timer
systemctl list-timers tormail-update.timer
journalctl -u tormail-update -n 50              # what the last run did
```
Prefer to approve updates yourself? Don't enable the timer; run `update.sh` by hand after reading the `--check` output.

## Publishing a release (maintainer, on your own computer)
```bash
git tag -s v1.1.0 -m "Tor-Mail 1.1.0"     # signed with your release key
git push origin main v1.1.0
```
Servers pick up the newest `vX.Y.Z` tag. Untagged commits on `main` are never deployed (with `CHANNEL=tags`).
To sign with an SSH key instead: `git config gpg.format ssh`, set `SIGNING_FORMAT=ssh` and `ALLOWED_SIGNERS` on the server.
(The SSH variant could not be tested in the development sandbox; the OpenPGP variant is covered by automated tests.)

## What each update run does
1. Takes a lock (two runs never overlap), refuses if the checkout has local changes.
2. Fetches the newest release tag into private refs (`refs/updater/*`): your own branches and tags are never touched.
3. **Verifies the signature**; refuses if it is missing or from a key not in the keyring.
4. **Refuses anything that is not a fast-forward** of the installed version (blocks rollback and rewritten-history attacks).
5. Takes an **encrypted database backup** (`age`); refuses to continue without one unless `SKIP_BACKUP=1`. Keeps the newest `BACKUP_KEEP`.
6. Checks out the release, `docker compose build --pull`, `docker compose up -d`, then waits until the app container is healthy.
7. If it never becomes healthy: puts the previous version back and exits non-zero (the journal shows `event=rollback_done`).

## Things updates will not do for you
- **Database migrations are not undone by a rollback.** Migrations run when the new container starts. If a bad release changed the schema, restore the pre-update backup into a fresh instance (see `docs/INSTALL.md`, section 7).
- **New settings.** If a release needs a new variable in `.env`, the containers will not become healthy and the update rolls back. Read the release notes (`--check` prints the commit list) and add the variable first.
- **Tor identity and secrets** are never touched (`.env` and the onion key volume are not in git).
- **Security of the signing key and your GitHub account** is on you. Turn on two-factor authentication there anyway.
- Users notice nothing special, but their browsers load new JavaScript after an update. If you publish file hashes (`/.well-known/assets.json`) for checking, they change with every release.

## Troubleshooting
| Output | Meaning |
|---|---|
| `update_refused reason=signature_missing_or_not_from_a_trusted_key_for_vX` | The tag is unsigned, or signed by a key not in `/etc/tormail/gnupg`. Check with `GNUPGHOME=/etc/tormail/gnupg git -C /opt/tor-mail verify-tag refs/updater/tags/vX` |
| `reason=not_a_fast_forward_...` | The release is not built on top of what is installed (history rewritten, or an older tag). Investigate before doing anything |
| `reason=local_modifications_present` | Someone edited tracked files in `/opt/tor-mail`. `git -C /opt/tor-mail status`, then `git checkout -- .` if the edits are unwanted |
| `reason=fetch_failed` | No network, wrong `REPO_URL`, or GitHub is down. Nothing was changed |
| `reason=set_AGE_RECIPIENT...` | Set `AGE_RECIPIENT` (a public key from `age-keygen`) or consciously set `SKIP_BACKUP=1` |
| `event=update_failed ... rollback_done` | The new version did not become healthy and the old one was restored. `docker compose logs app` shows why |
| `event=rollback_failed` | Both versions are unhealthy. Restore from backup and fix by hand |
