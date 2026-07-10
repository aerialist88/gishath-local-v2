#!/usr/bin/env bash
# publish_gallery.sh — bake the public gallery and push it to GitHub Pages.
#
# Re-runs atelier.publish (regenerating gallery_site/ from the deck output),
# then commits and pushes the result to the dedicated gallery repo. The nightly
# run calls this after each deck so friends always see the latest.
#
# ONE-TIME SETUP (until this is done, the push step is skipped harmlessly):
#   1. Create an EMPTY repo on github.com — e.g. "deckwright-gallery"
#      (public; no README/.gitignore, we already have commits).
#   2. Point this local site repo at it and do the first push by hand — that
#      also primes the macOS keychain so future nightly pushes need no prompt:
#        cd gallery_site
#        git remote add origin https://github.com/<you>/deckwright-gallery.git
#        git config credential.helper osxkeychain
#        git push -u origin main
#   3. On github.com: Settings -> Pages -> Source: "Deploy from a branch",
#      Branch: main / root. The site goes live at
#        https://<you>.github.io/deckwright-gallery/
#
# After that, `./publish_gallery.sh` (and the nightly run) keep it current.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# shellcheck disable=SC1091
source venv/bin/activate
python -m atelier.publish

cd gallery_site
if [ ! -d .git ]; then
  echo "gallery: gallery_site/ isn't a git repo — skipping push (run the one-time setup in this script's header)."
  exit 0
fi

git add -A
if git diff --cached --quiet; then
  echo "gallery: no changes to publish."
  exit 0
fi

git -c user.name="Trevor Jow" -c user.email="trevorjow@hotmail.com" \
    commit -q -m "Gallery update $(date '+%Y-%m-%d %H:%M')"

if git remote get-url origin > /dev/null 2>&1; then
  git push -q origin HEAD && echo "gallery: pushed to GitHub Pages."
else
  echo "gallery: committed locally, but no 'origin' remote yet — see the one-time setup above."
fi
