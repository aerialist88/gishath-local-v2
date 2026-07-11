#!/bin/zsh
# Compile forge_bridge/AtelierSim.java against the Forge release jar using the
# portable JDK — no Maven, no system Java. Output lands in
# third_party/forge/atelier-classes/, which atelier/forge_engine.py prefers
# over Forge's stock `sim` mode when present.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JAVAC=$(echo "$ROOT"/third_party/jdk-*/Contents/Home/bin/javac)
JAR="$ROOT/third_party/forge/forge-gui-desktop-2.0.13-jar-with-dependencies.jar"
OUT="$ROOT/third_party/forge/atelier-classes"

[ -x "$JAVAC" ] || { echo "portable JDK not found under third_party/ — see atelier/forge_engine.py" >&2; exit 1; }
[ -f "$JAR" ] || { echo "Forge jar not found: $JAR" >&2; exit 1; }

mkdir -p "$OUT"
"$JAVAC" -cp "$JAR" -d "$OUT" "$ROOT/forge_bridge/AtelierSim.java"
echo "built: $OUT/AtelierSim.class"
