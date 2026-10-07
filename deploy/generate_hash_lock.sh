#!/usr/bin/env bash
#
# generate_hash_lock.sh - produces requirements.lock.txt with real
# cryptographic hashes for every pinned dependency (and their transitive
# dependencies), so a future `pip install` can verify it's getting the
# exact same package bytes every time, not just the same version number.
#
# Why this is a SEPARATE script you run yourself, not something already
# done in this repo: generating real hashes means downloading the actual
# package files from PyPI to compute their hashes - this requires network
# access, which the environment this project was built in does not have.
# Fabricating hash values here would be worse than not having them at all,
# so this is left as a real, verifiable step for you to run once (and
# again whenever requirements.txt changes) in an environment with normal
# internet access.
#
# ─── Usage ───────────────────────────────────────────────────────────────
#   pip install pip-tools --break-system-packages   # one-time, if you don't have it
#   ./deploy/generate_hash_lock.sh
#
# This produces requirements.lock.txt in the project root. From then on,
# install with:
#   pip install -r requirements.lock.txt --require-hashes --break-system-packages
#
# --require-hashes makes pip REFUSE to install anything (including
# transitive dependencies) that doesn't match a known-good hash - this is
# what actually closes the gap flagged in the review (version pinning alone
# doesn't guarantee you get the exact same bytes on a future reinstall;
# hash pinning does).
#
# Re-run this script whenever requirements.txt changes, and commit the
# updated requirements.lock.txt alongside it.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v pip-compile &> /dev/null; then
    echo "ERROR: pip-compile not found. Install it first:" >&2
    echo "  pip install pip-tools --break-system-packages" >&2
    exit 1
fi

echo "Generating requirements.lock.txt with real package hashes from PyPI..."
pip-compile --generate-hashes --output-file=requirements.lock.txt requirements.txt

echo "Done. requirements.lock.txt written."
echo "Install with: pip install -r requirements.lock.txt --require-hashes --break-system-packages"
