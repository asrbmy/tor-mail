#!/usr/bin/env bash
# Tor-Mail host-side updater. Run it on the HOST (by hand or from the systemd timer in deployment/), never inside a
# container: the containers must not be able to reach the Docker socket.
#
# What it does, in order, and refuses at the first problem:
#   1. fetches the newest release tag (or branch tip) from REPO_URL into a private ref namespace
#   2. verifies a signature made by a key YOU trust (dedicated keyring / allowed-signers file). Unsigned = refused
#   3. refuses anything that is not a pure fast-forward of what is installed (no rollback or force-push attacks)
#   4. refuses to run over local modifications
#   5. takes an encrypted database backup (required unless you explicitly skip it)
#   6. checks out the new version, rebuilds, restarts, waits for the app to report healthy
#   7. if it does not become healthy: puts the previous version back and exits non-zero
# Database migrations run when the container starts and are NOT undone by a rollback; the backup is for that case.
#
#   scripts/update.sh            check, then apply an update if there is one
#   scripts/update.sh --check    only report (exit 10 = a verified update is available, 0 = up to date)
#
# Settings come from the environment or from $TORMAIL_UPDATE_ENV (default /etc/tormail/update.env). See
# deployment/update.env.example. Output is event=... lines, safe for the journal (no secrets are printed).
set -euo pipefail
umask 077

ENV_FILE="${TORMAIL_UPDATE_ENV:-/etc/tormail/update.env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_URL="${REPO_URL:-https://github.com/asrbmy/tor-mail.git}"
CHANNEL="${CHANNEL:-tags}"                        # tags: newest vX.Y.Z release tag. branch: tip of BRANCH
BRANCH="${BRANCH:-main}"
REQUIRE_SIGNED="${REQUIRE_SIGNED:-1}"             # 0 only for testing; the script says so loudly
SIGNING_FORMAT="${SIGNING_FORMAT:-openpgp}"       # openpgp | ssh
ALLOWED_SIGNERS="${ALLOWED_SIGNERS:-/etc/tormail/allowed_signers}"   # ssh format only
export GNUPGHOME="${GNUPGHOME:-/etc/tormail/gnupg}"                   # openpgp format: keyring holding ONLY release keys
BACKUP_DIR="${BACKUP_DIR:-/var/backups/tormail}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
AGE_RECIPIENT="${AGE_RECIPIENT:-}"                # age public key that encrypts the backups
SKIP_BACKUP="${SKIP_BACKUP:-0}"
BACKUP_CMD="${BACKUP_CMD:-}"                      # default: pg_dump inside the db container
COMPOSE="${COMPOSE:-docker compose}"
DOCKER="${DOCKER:-docker}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-3}"
MODE=apply
[ "${1:-}" = "--check" ] && MODE=check

log() { echo "event=$1${2:+ $2}"; }
die() { log "update_refused" "reason=$1"; exit 1; }

cd "$INSTALL_DIR"
[ -d .git ] || die "install_dir_is_not_a_git_checkout"

exec 9>"$INSTALL_DIR/.update.lock"
flock -n 9 || { log update_skipped "reason=another_update_is_running"; exit 0; }

GIT=(git -c core.hooksPath=/dev/null)
if [ "$REQUIRE_SIGNED" = "1" ]; then
  case "$SIGNING_FORMAT" in
    openpgp) [ -d "$GNUPGHOME" ] || die "no_release_keyring_at_$GNUPGHOME" ;;
    ssh)
      [ -s "$ALLOWED_SIGNERS" ] || die "no_allowed_signers_file"
      GIT+=(-c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$ALLOWED_SIGNERS") ;;
    *) die "unknown_signing_format" ;;
  esac
else
  log insecure_config "detail=signature_checking_disabled"
fi

if [ -n "$("${GIT[@]}" status --porcelain --untracked-files=no)" ]; then
  die "local_modifications_present"
fi

CURRENT="$("${GIT[@]}" rev-parse HEAD)"
CURRENT_NAME="$("${GIT[@]}" describe --tags --always 2>/dev/null || echo "$CURRENT")"

# Fetch into a private namespace so nothing here can overwrite or move your own branches and tags.
if [ "$CHANNEL" = "tags" ]; then
  "${GIT[@]}" fetch --quiet --no-tags --prune "$REPO_URL" '+refs/tags/v*:refs/updater/tags/v*' || die "fetch_failed"
  LATEST="$("${GIT[@]}" for-each-ref --sort=-v:refname --count=1 --format='%(refname)' refs/updater/tags/)"
  [ -n "$LATEST" ] || { log update_none "detail=no_release_tags_found"; exit 0; }
  TARGET_NAME="${LATEST#refs/updater/tags/}"
  TARGET="$("${GIT[@]}" rev-parse "$LATEST^{commit}")"
  VERIFY=(verify-tag "$LATEST")
else
  "${GIT[@]}" fetch --quiet --no-tags "$REPO_URL" "+refs/heads/$BRANCH:refs/updater/heads/$BRANCH" || die "fetch_failed"
  TARGET="$("${GIT[@]}" rev-parse "refs/updater/heads/$BRANCH")"
  TARGET_NAME="$BRANCH@${TARGET:0:12}"
  VERIFY=(verify-commit "$TARGET")
fi

if [ "$TARGET" = "$CURRENT" ]; then
  log update_none "installed=$CURRENT_NAME"
  exit 0
fi

if [ "$REQUIRE_SIGNED" = "1" ]; then
  if ! "${GIT[@]}" "${VERIFY[@]}" >/dev/null 2>&1; then
    die "signature_missing_or_not_from_a_trusted_key_for_${TARGET_NAME}"
  fi
fi

# Only ever move forward along the same history.
"${GIT[@]}" merge-base --is-ancestor "$CURRENT" "$TARGET" || die "not_a_fast_forward_from_${CURRENT_NAME}_to_${TARGET_NAME}"

if [ "$MODE" = "check" ]; then
  log update_available "from=$CURRENT_NAME to=$TARGET_NAME signature=$([ "$REQUIRE_SIGNED" = 1 ] && echo verified || echo not_checked)"
  "${GIT[@]}" log --oneline --no-decorate "$CURRENT..$TARGET" | head -30 | sed 's/^/change=/'
  exit 10
fi

# ---- backup -------------------------------------------------------------------------------------------------
BACKUP_FILE=""
if [ "$SKIP_BACKUP" != "1" ]; then
  [ -n "$AGE_RECIPIENT" ] || die "set_AGE_RECIPIENT_to_encrypt_backups_or_SKIP_BACKUP=1_if_you_accept_the_risk"
  command -v age >/dev/null 2>&1 || die "age_is_not_installed"
  mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
  BACKUP_FILE="$BACKUP_DIR/pre-update-$(date -u +%Y%m%dT%H%M%SZ)-${CURRENT:0:12}.dump.age"
  if [ -n "$BACKUP_CMD" ]; then
    bash -c "$BACKUP_CMD" | age -r "$AGE_RECIPIENT" > "$BACKUP_FILE" || { rm -f "$BACKUP_FILE"; die "backup_failed"; }
  else
    $COMPOSE exec -T db pg_dump -U tormail -Fc tormail | age -r "$AGE_RECIPIENT" > "$BACKUP_FILE" || { rm -f "$BACKUP_FILE"; die "backup_failed"; }
  fi
  [ -s "$BACKUP_FILE" ] || { rm -f "$BACKUP_FILE"; die "backup_is_empty"; }
  log backup_done "file=$BACKUP_FILE"
  # Old pre-update backups are removed so that deleted accounts do not live on in them forever.
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/pre-update-*.dump.age 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f --
fi

# ---- deploy -------------------------------------------------------------------------------------------------
app_healthy() {
  local cid status
  cid="$($COMPOSE ps -q app 2>/dev/null | head -1)"
  [ -n "$cid" ] || return 1
  status="$($DOCKER inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || true)"
  [ "$status" = "healthy" ]
}
wait_healthy() {
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    app_healthy && return 0
    sleep "$HEALTH_INTERVAL"; waited=$((waited + HEALTH_INTERVAL))
  done
  return 1
}
deploy() {   # $1 = commit
  "${GIT[@]}" checkout --quiet --detach "$1"
  $COMPOSE build --pull
  $COMPOSE up -d --remove-orphans
}

log update_start "from=$CURRENT_NAME to=$TARGET_NAME"
if deploy "$TARGET" && wait_healthy; then
  log update_done "installed=$TARGET_NAME"
  exit 0
fi

log update_failed "detail=new_version_did_not_become_healthy action=rolling_back"
if deploy "$CURRENT" && wait_healthy; then
  log rollback_done "installed=$CURRENT_NAME note=database_migrations_are_not_undone backup=${BACKUP_FILE:-none}"
else
  log rollback_failed "detail=previous_version_is_not_healthy_either action=manual_intervention_needed"
fi
exit 1
