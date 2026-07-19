#!/bin/bash
# Double-click launcher for The Foundry (native desktop window).
# Safe to move this file anywhere (Desktop, Dock, Applications) — the project
# path below is absolute, so it always finds the app.

cd "/Users/trevorjow/Desktop/Cowork Playground/Local Gishath Fetch/gishath-local-lmstudio" || {
  echo "Could not find the gishath-local-lmstudio project folder."
  read -p "Press Return to close this window..."
  exit 1
}

echo "Starting The Foundry..."
echo ""
source venv/bin/activate
python -m atelier.desktop
status=$?

if [ $status -ne 0 ]; then
  echo ""
  echo "⚠️  Atelier exited with an error (code $status) — see above."
  read -p "Press Return to close this window..."
fi
