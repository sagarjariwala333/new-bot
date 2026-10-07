#!/usr/bin/env bash
#
# backup_data.sh - backs up the bot's data/ directory (encrypted account
# credentials, pair config, master encryption key, trade ledger, auth.json)
# so a dead/destroyed server doesn't mean starting over from scratch.
#
# Run this on a schedule via cron (see the crontab line below) - it does
# NOT touch the bot process at all, so it's safe to run while the bot is
# live and trading.
#
# ─── 2026-09-14 fixes (per a third-party review) ─────────────────────────
#   1. The master encryption key (data/.master.key) is now backed up
#      SEPARATELY from everything else, in its own archive. Previously both
#      lived in the same file - convenient, but it meant a single stolen
#      backup was enough to decrypt every stored API credential. Store the
#      two archives in DIFFERENT locations (e.g. the main archive off-site
#      via REMOTE_DEST, the key archive somewhere else entirely - a
#      password manager, a separate secrets store, a different provider)
#      so no single leak is sufficient on its own.
#   2. After creating the main archive, every ledger .jsonl file inside it
#      is now validated (each line must parse as JSON) before the script
#      reports success - ledger files are append-only and not written
#      atomically, so a backup running at the exact moment of a write could
#      in principle catch a partially-written line. This won't PREVENT that
#      (no file locking was added), but it means you find out immediately
#      if a specific backup was affected, rather than discovering a corrupt
#      ledger entry much later during a restore.
#
# ─── Setup ───────────────────────────────────────────────────────────────
#   1. Edit BOT_DIR and BACKUP_DIR below for your server.
#   2. Optionally set REMOTE_DEST to copy the MAIN backup off this server
#      (e.g. an rsync target, another host, or a mounted cloud-storage
#      path) - a backup that only lives on the same server that might die
#      isn't really a backup. Set KEY_REMOTE_DEST separately (and
#      different!) for the key archive - see point 1 above.
#   3. chmod +x deploy/backup_data.sh
#   4. Add to crontab (runs every 6 hours, keeps the last 28 = ~1 week):
#        crontab -e
#        0 */6 * * * /opt/hull-futures-bot/deploy/backup_data.sh >> /var/log/hull-bot-backup.log 2>&1
#
# ─── Restoring on a new server ───────────────────────────────────────────
#   1. Set up the new server the same way (see README.md).
#   2. Extract BOTH archives into the new server's `data/` directory:
#        tar -xzf hull-bot-data-<timestamp>.tar.gz -C /opt/hull-futures-bot/
#        tar -xzf hull-bot-key-<timestamp>.tar.gz -C /opt/hull-futures-bot/
#      The main archive alone is NOT enough to decrypt the stored API
#      credentials - you need the key archive too, by design (see point 1).
#   3. Start the bot - it picks up right where the old server's config left
#      off (accounts, pairs, ledger history). Positions/orders themselves
#      live on Binance, not in this backup, so nothing about live trades is
#      lost either way - this backup is purely so you don't have to
#      re-enter API keys and pair settings from scratch.

set -euo pipefail

BOT_DIR="/opt/hull-futures-bot"          # EDIT: where the bot is installed
BACKUP_DIR="/var/backups/hull-futures-bot"  # EDIT: where to keep local backups
REMOTE_DEST=""                            # EDIT (optional): main archive off-site copy
KEY_REMOTE_DEST=""                        # EDIT (optional, and DIFFERENT from REMOTE_DEST): key archive off-site copy
ALLOW_SAME_REMOTE_DEST=0                  # EDIT only if you have a specific reason to send both archives to the
                                           # same destination (see the check below) - defaults to rejecting that
KEEP_LAST=28                              # how many local backups to retain before pruning older ones

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_NAME="hull-bot-data-${TIMESTAMP}.tar.gz"
KEY_ARCHIVE_NAME="hull-bot-key-${TIMESTAMP}.tar.gz"

# 2026-09-14 fix (item 14, per a third-party review): the whole point of a
# separate key archive is that stealing ONE of the two isn't enough to
# decrypt anything - an operator accidentally pointing both REMOTE_DEST and
# KEY_REMOTE_DEST at the same place silently defeats that. Reject this by
# default rather than just documenting "these should differ" and hoping.
if [ -n "$REMOTE_DEST" ] && [ -n "$KEY_REMOTE_DEST" ] && [ "$REMOTE_DEST" = "$KEY_REMOTE_DEST" ] \
   && [ "$ALLOW_SAME_REMOTE_DEST" != "1" ]; then
    echo "ERROR: REMOTE_DEST and KEY_REMOTE_DEST are set to the exact same destination." >&2
    echo "This defeats the point of keeping the encryption key separate from the credentials -" >&2
    echo "anyone with access to that one destination would have everything needed to decrypt." >&2
    echo "Set ALLOW_SAME_REMOTE_DEST=1 above if you have a specific reason to do this anyway." >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

if [ ! -d "$BOT_DIR/data" ]; then
    echo "ERROR: $BOT_DIR/data does not exist - check BOT_DIR is set correctly." >&2
    exit 1
fi

# ─── Master key: its own archive, backed up separately ────────────────────
if [ -f "$BOT_DIR/data/.master.key" ]; then
    echo "[$(date)] Backing up master key -> ${BACKUP_DIR}/${KEY_ARCHIVE_NAME}"
    tar -czf "${BACKUP_DIR}/${KEY_ARCHIVE_NAME}" -C "$BOT_DIR/data" ".master.key"
    chmod 600 "${BACKUP_DIR}/${KEY_ARCHIVE_NAME}"
else
    echo "[$(date)] No data/.master.key found (MASTER_ENC_KEY is likely set directly in .env instead) - skipping key archive."
fi

# ─── Everything else: accounts/pairs config, ledger, auth ─────────────────
echo "[$(date)] Backing up ${BOT_DIR}/data -> ${BACKUP_DIR}/${ARCHIVE_NAME}"
tar -czf "${BACKUP_DIR}/${ARCHIVE_NAME}" -C "$BOT_DIR" \
    --exclude="data/.master.key" \
    data

# Restrict permissions - this archive contains encrypted API credentials;
# treat it with the same care as the original data/ directory.
chmod 600 "${BACKUP_DIR}/${ARCHIVE_NAME}"

# ─── Validate: every ledger .jsonl line must parse as JSON ────────────────
echo "[$(date)] Validating ledger files in the new archive..."
VALIDATION_TMP="$(mktemp -d)"
trap 'rm -rf "$VALIDATION_TMP"' EXIT
VALIDATION_FAILED=0
if ! tar -xzf "${BACKUP_DIR}/${ARCHIVE_NAME}" -C "$VALIDATION_TMP" 2>/tmp/hull_backup_extract_err.$$; then
    echo "[$(date)] ERROR: could not extract the archive just created for validation - treating this backup as FAILED, not just logging and moving on." >&2
    cat /tmp/hull_backup_extract_err.$$ >&2
    VALIDATION_FAILED=1
fi
rm -f /tmp/hull_backup_extract_err.$$
BAD_LINES=0
if [ -d "$VALIDATION_TMP/data/ledger" ]; then
    for f in "$VALIDATION_TMP/data/ledger"/*.jsonl; do
        [ -e "$f" ] || continue
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            if ! python3 -c "import json,sys; json.loads(sys.argv[1])" "$line" 2>/dev/null; then
                echo "[$(date)] WARNING: malformed JSON line in $(basename "$f") - likely caught mid-write. Rerun the backup; this specific archive's ledger for that file may be short one entry." >&2
                BAD_LINES=$((BAD_LINES + 1))
            fi
        done < "$f"
    done
fi
if [ "$BAD_LINES" -gt 0 ]; then
    VALIDATION_FAILED=1
fi

# 2026-09-14 fix (per a third-party review): validation failures used to
# only be LOGGED - the script still reported "Backup complete" and still
# shipped the archive to REMOTE_DEST regardless. Now genuinely fails
# closed: a failed archive is renamed with a .SUSPECT suffix (kept, not
# deleted, in case it's still partially useful for troubleshooting - but
# unmistakably NOT a normal trusted backup), never copied off-site, and
# the script exits nonzero so cron/monitoring actually notices.
if [ "$VALIDATION_FAILED" -eq 1 ]; then
    echo "[$(date)] Ledger validation FAILED (${BAD_LINES} malformed line(s), extraction ok: $([ "$VALIDATION_FAILED" -eq 1 ] && echo see above)) - this backup will NOT be copied off-site."
    mv "${BACKUP_DIR}/${ARCHIVE_NAME}" "${BACKUP_DIR}/${ARCHIVE_NAME}.SUSPECT"
    echo "[$(date)] Renamed to ${ARCHIVE_NAME}.SUSPECT - kept locally for troubleshooting, not treated as a valid backup. Re-run the backup to get a clean one."
    exit 1
fi
echo "[$(date)] Ledger validation OK - no malformed lines found."

# ─── Off-site copies (kept deliberately separate from each other) ─────────
if [ -n "$REMOTE_DEST" ]; then
    echo "[$(date)] Copying main archive to remote destination: ${REMOTE_DEST}"
    rsync -avz "${BACKUP_DIR}/${ARCHIVE_NAME}" "$REMOTE_DEST" || \
        echo "[$(date)] WARNING: remote copy failed - local backup still saved at ${BACKUP_DIR}/${ARCHIVE_NAME}" >&2
fi
if [ -n "$KEY_REMOTE_DEST" ] && [ -f "${BACKUP_DIR}/${KEY_ARCHIVE_NAME}" ]; then
    echo "[$(date)] Copying key archive to remote destination: ${KEY_REMOTE_DEST}"
    rsync -avz "${BACKUP_DIR}/${KEY_ARCHIVE_NAME}" "$KEY_REMOTE_DEST" || \
        echo "[$(date)] WARNING: key remote copy failed - local key backup still saved at ${BACKUP_DIR}/${KEY_ARCHIVE_NAME}" >&2
fi

# ─── Prune old local backups beyond KEEP_LAST, oldest first (both kinds) ──
for PATTERN in "hull-bot-data-*.tar.gz" "hull-bot-key-*.tar.gz"; do
    BACKUP_COUNT=$(ls -1 "${BACKUP_DIR}"/${PATTERN} 2>/dev/null | wc -l)
    if [ "$BACKUP_COUNT" -gt "$KEEP_LAST" ]; then
        TO_DELETE=$((BACKUP_COUNT - KEEP_LAST))
        echo "[$(date)] Pruning ${TO_DELETE} old ${PATTERN} backup(s), keeping the most recent ${KEEP_LAST}."
        ls -1t "${BACKUP_DIR}"/${PATTERN} | tail -n "$TO_DELETE" | xargs -r rm -f
    fi
done

echo "[$(date)] Backup complete."
