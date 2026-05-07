#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
PLIST="$HOME/Library/LaunchAgents/com.amlradar.daily.plist"
PYTHON="$VENV/bin/python3"
LOG_OUT="$DIR/amlradar.log"
LOG_ERR="$DIR/amlradar.err"

echo "── AML Radar setup ──────────────────────────────────────"
echo "Project dir : $DIR"
echo "Virtualenv  : $VENV"

# ── 1. Virtualenv ──────────────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
  echo "Creating virtualenv..."
  python3 -m venv "$VENV"
fi

echo "Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet requests beautifulsoup4 lxml

echo "✓ Dependencies installed"

# ── 2. Verify syntax ───────────────────────────────────────────────────────────
echo "Checking Python syntax..."
for f in config.py db.py scrapers.py report.py main.py; do
  "$PYTHON" -c "import ast; ast.parse(open('$DIR/$f').read()); print('  OK $f')"
done

# ── 3. Init DB ─────────────────────────────────────────────────────────────────
echo "Initialising database..."
cd "$DIR" && "$PYTHON" -c "from db import init_db; init_db(); print('  OK amlradar.db')"

# ── 4. launchd plist ───────────────────────────────────────────────────────────
echo "Writing launchd plist → $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.amlradar.daily</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${DIR}/main.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${DIR}</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>   <integer>7</integer>
    <key>Minute</key> <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_OUT}</string>

  <key>StandardErrorPath</key>
  <string>${LOG_ERR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>GMAIL_APP_PASSWORD</key> <string>${GMAIL_APP_PASSWORD:-}</string>
    <key>AML_EMAIL_FROM</key>     <string>${AML_EMAIL_FROM:-you@gmail.com}</string>
    <key>AML_EMAIL_TO</key>       <string>${AML_EMAIL_TO:-you@gmail.com}</string>
  </dict>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
PLIST

# ── 5. Load / reload job ───────────────────────────────────────────────────────
if launchctl list | grep -q "com.amlradar.daily"; then
  echo "Reloading existing launchd job..."
  launchctl unload "$PLIST" 2>/dev/null || true
fi

launchctl load "$PLIST"
echo "✓ launchd job loaded: com.amlradar.daily (runs daily at 07:00)"

# ── 6. Smoke test ─────────────────────────────────────────────────────────────
echo ""
echo "── Smoke test (--status) ────────────────────────────────"
cd "$DIR" && "$PYTHON" main.py --status

echo ""
echo "── Done ─────────────────────────────────────────────────"
echo "  Set env vars before running:"
echo "    export GMAIL_APP_PASSWORD='your-app-password'"
echo "    export AML_EMAIL_FROM='you@gmail.com'"
echo "    export AML_EMAIL_TO='you@gmail.com'"
echo ""
echo "  Manual run:      cd $DIR && $PYTHON main.py"
echo "  Dry run:         cd $DIR && $PYTHON main.py --dry"
echo "  Check status:    cd $DIR && $PYTHON main.py --status"
echo "  Logs:            tail -f $LOG_OUT"
