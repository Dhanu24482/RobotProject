#!/usr/bin/env bash
# Lumi remote bridge - see the robot's ROS 2 topics on THIS machine, through the Cloudflare Tunnel.
#
# Made for headless Linux machines. The first run installs what it needs (once), signs you in to
# Cloudflare Access, starts the tunnel and a Zenoh bridge in the BACKGROUND and exits. It never starts
# rviz2: it prints the command, so you start it when (and if) you want.
#
#   bash lumi-rviz.sh [HOST]         set up (first run) and start in the background
#   bash lumi-rviz.sh status         what is running, is the robot reachable
#   bash lumi-rviz.sh topics         the topics the robot shares right now
#   bash lumi-rviz.sh test           check that topic DATA arrives (needs ROS 2 installed here)
#   bash lumi-rviz.sh diag           one report to paste when something is wrong
#   bash lumi-rviz.sh logs [tunnel|bridge|supervisor]
#   bash lumi-rviz.sh rviz [args]    open rviz2 the right way (starts the bridge if needed; args go to rviz2)
#   bash lumi-rviz.sh login          sign in to Cloudflare Access again (the sign-in expires)
#   bash lumi-rviz.sh stop | restart | update
#
# Unattended sign-in instead of the browser link: an Access SERVICE TOKEN allowed in the hostname's policy
#   CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... bash lumi-rviz.sh HOST
#
# Everything lives in ~/.lumi-rviz (override: LUMI_HOME). DDS domain here: LUMI_ROS_DOMAIN (default __DOMAIN__).
# Needs: bash, python3, and one of curl / wget. Linux only.
set -o pipefail

HOST_DEFAULT="__HOST__"
PORT=__PORT__
ZVER=__ZVER__
ROBOT_DISTRO=__DISTRO__
DOMAIN="${LUMI_ROS_DOMAIN:-__DOMAIN__}"
BASE="${LUMI_HOME:-$HOME/.lumi-rviz}"
BIN="$BASE/bin"; RUN="$BASE/run"; LOGS="$BASE/logs"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/$(basename "${BASH_SOURCE[0]}")"

if [ -t 1 ]; then B=$'\033[1m'; Y=$'\033[33m'; R=$'\033[31m'; G=$'\033[32m'; N=$'\033[0m'; else B=; Y=; R=; G=; N=; fi
say()  { printf '%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s ok %s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s !! %s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%s xx %s %s\n' "$R" "$N" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- command + host from the arguments
CMD=start; HOST_ARG=""; RVIZ_ARGS=()
if [ "${1:-}" = rviz ]; then shift; RVIZ_ARGS=("$@"); set --; WANT_RVIZ=1; fi   # everything after "rviz" belongs to rviz2
for a in "$@"; do
  case "$a" in
    start|stop|restart|status|topics|test|diag|logs|login|update|unsetup|help) [ "$CMD" = start ] && CMD="$a" || LOGWHICH="$a" ;;
    --check|-c) CMD=topics ;;
    --no-test) NO_TEST=1 ;;
    -h|--help) CMD=help ;;
    tunnel|bridge|supervisor) LOGWHICH="$a" ;;
    *) HOST_ARG="$a" ;;
  esac
done
[ -n "${WANT_RVIZ:-}" ] && CMD=rviz
[ "$CMD" = help ] && { sed -n '2,23p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0; }

mkdir -p "$BIN" "$RUN" "$LOGS"; chmod 700 "$BASE" 2>/dev/null
HOST="${HOST_ARG:-$(cat "$BASE/host" 2>/dev/null)}"; HOST="${HOST:-$HOST_DEFAULT}"
APP="https://$HOST"

# ---------------------------------------------------------------- download helpers
fetch() {   # fetch URL OUTFILE
  if have curl; then curl -fL --retry 3 --connect-timeout 20 -sS -o "$2" "$1"
  elif have wget; then wget -q -O "$2" "$1"
  else python3 - "$1" "$2" <<'PY'
import sys, urllib.request, shutil
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2], 'wb') as f:
    shutil.copyfileobj(r, f)
PY
  fi
}
cpu() { uname -m; }
cf_asset() {
  case "$(cpu)" in
    x86_64|amd64) echo amd64 ;;  aarch64|arm64) echo arm64 ;;
    armv7*|armhf) echo armhf ;;  armv6*|arm) echo arm ;;  i?86) echo 386 ;;
    *) echo "" ;;
  esac
}
zenoh_asset() {
  case "$(cpu)" in x86_64|amd64) echo x86_64 ;; aarch64|arm64) echo aarch64 ;; *) echo "" ;; esac
}

ensure_tools() {
  have python3 || die "python3 is required (e.g. sudo apt install python3)."
  have curl || have wget || true      # python3 can download too
}

find_cloudflared() {      # sets CF if cloudflared exists; never downloads
  if [ -x "$BIN/cloudflared" ]; then CF="$BIN/cloudflared"; return 0; fi
  if have cloudflared; then CF="$(command -v cloudflared)"; return 0; fi
  CF=""; return 1
}

ensure_cloudflared() {
  if [ -x "$BIN/cloudflared" ] && "$BIN/cloudflared" --version >/dev/null 2>&1; then CF="$BIN/cloudflared"; return; fi
  if have cloudflared; then CF="$(command -v cloudflared)"; return; fi          # already installed system-wide
  local a; a="$(cf_asset)"; [ -n "$a" ] || die "no cloudflared build for CPU '$(cpu)'. Install it manually: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  say "Installing cloudflared (latest release, linux-$a) - one time only"
  echo "   downloading about 40 MB from github.com ..."
  fetch "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$a" "$BIN/cloudflared.part" \
    || { rm -f "$BIN/cloudflared.part"; die "could not download cloudflared (is github.com reachable from here?)"; }
  chmod +x "$BIN/cloudflared.part"
  "$BIN/cloudflared.part" --version >/dev/null 2>&1 || { rm -f "$BIN/cloudflared.part"; die "the downloaded cloudflared does not run on this machine ($(cpu))."; }
  mv -f "$BIN/cloudflared.part" "$BIN/cloudflared"; CF="$BIN/cloudflared"
  ok "installed $("$CF" --version 2>&1 | head -1)"
}

ensure_bridge() {
  if [ -x "$BIN/zenoh-bridge-ros2dds" ] && "$BIN/zenoh-bridge-ros2dds" --version >/dev/null 2>&1; then BRIDGE="$BIN/zenoh-bridge-ros2dds"; return; fi
  if have zenoh-bridge-ros2dds; then BRIDGE="$(command -v zenoh-bridge-ros2dds)"; return; fi
  local a; a="$(zenoh_asset)"; [ -n "$a" ] || die "no prebuilt zenoh-bridge-ros2dds for CPU '$(cpu)'."
  say "Installing zenoh-bridge-ros2dds $ZVER ($a) - one time only"
  local t; t="$(mktemp -d)"
  fetch "https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds/releases/download/$ZVER/zenoh-plugin-ros2dds-$ZVER-$a-unknown-linux-gnu-standalone.zip" "$t/z.zip" \
    || { rm -rf "$t"; die "could not download the Zenoh bridge"; }
  python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('zenoh-bridge-ros2dds', sys.argv[2])" "$t/z.zip" "$t" \
    || { rm -rf "$t"; die "could not unpack the Zenoh bridge"; }
  install -m 755 "$t/zenoh-bridge-ros2dds" "$BIN/zenoh-bridge-ros2dds"; rm -rf "$t"
  "$BIN/zenoh-bridge-ros2dds" --version >/dev/null 2>&1 || die "the Zenoh bridge does not run on this machine."
  BRIDGE="$BIN/zenoh-bridge-ros2dds"; ok "installed zenoh-bridge-ros2dds $ZVER"
}

# ---------------------------------------------------------------- state helpers
pid_alive() { [ -s "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
sup_running() { pid_alive "$RUN/supervisor.pid"; }
port_listening() { python3 - "$PORT" <<'PY'
import sys
p = int(sys.argv[1])
for f in ('/proc/net/tcp', '/proc/net/tcp6'):
    try:
        for l in open(f).read().splitlines()[1:]:
            c = l.split()
            if c[3] == '0A' and int(c[1].split(':')[1], 16) == p:
                sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PY
}
using_service_token() { [ -n "$CF_ACCESS_CLIENT_ID" ] && [ -n "$CF_ACCESS_CLIENT_SECRET" ]; }
signed_in() { using_service_token || "$CF" access token -app="$APP" >/dev/null 2>&1; }
redact() { sed -E 's/((token|aud|secret|client[_-]?secret)=)[^&" ]+/\1<redacted>/Ig'; }

# Parses bridge.log. Every client on the tunnel (a second PC, or rviz2's own bridge) also shows up in the log as a "bridge",
# so the ROBOT is taken to be the one that announces by far the most items (hundreds, against a handful for a client).
#   bridge_report status -> NONE | CONNECTED | DISCONNECTED   (is the robot's bridge there right now)
#   bridge_report items  -> "kind<TAB>/name" for everything the robot currently shares
bridge_report() { python3 - "$LOGS/bridge.log" "$1" <<'PY'
import re, sys
mode = sys.argv[2]
try:
    txt = re.sub(r'\x1b\[[0-9;]*m', '', open(sys.argv[1], errors='replace').read())
except OSError:
    if mode == 'status':
        print('NONE')
    sys.exit(0)
present, live, count = {}, {}, {}
for line in txt.splitlines():
    m = re.search(r'New ROS 2 bridge detected: ([0-9a-f]+)', line)
    if m:
        present[m.group(1)] = True
        continue
    m = re.search(r'Remote ROS 2 bridge left: ([0-9a-f]+)', line)
    if m:
        present[m.group(1)] = False
        live.pop(m.group(1), None)
        continue
    m = re.search(r'Remote bridge ([0-9a-f]+) (announces|retires) (Publisher|Subscriber|Service Server|Service Client|Action Server|Action Client) (\S+)', line)
    if m:
        i, verb, kind, name = m.groups()
        key = (kind, '/' + name.lstrip('/'))
        if verb == 'announces':
            live.setdefault(i, set()).add(key)
            count[i] = count.get(i, 0) + 1
        else:
            live.get(i, set()).discard(key)
robot = max(count, key=count.get) if count else next(iter(present), None)
if mode == 'status':
    print('NONE' if robot is None else ('CONNECTED' if present.get(robot) else 'DISCONNECTED'))
else:
    for kind, name in sorted(live.get(robot, ())):
        print('%s\t%s' % (kind, name))
PY
}
shared_topics() { bridge_report items; }
# "connected" = the tunnel is open AND the robot's own bridge is present (other clients coming and going do not count)
bridge_connected() { port_listening && [ "$(bridge_report status)" = CONNECTED ]; }
print_topics() {
  local out; out="$(shared_topics)"
  [ -n "$out" ] || { echo "   (the robot shares no topics right now)"; return 1; }
  for kind in "Publisher|Topics the robot SENDS (you can read)" "Subscriber|Topics you can SEND to the robot" \
              "Service Server|Services" "Action Server|Actions you can call"; do
    local k="${kind%%|*}" t="${kind#*|}"
    local names; names="$(printf '%s\n' "$out" | awk -F'\t' -v k="$k" '$1==k{print "     " $2}')"
    [ -n "$names" ] && { printf '\n   %s:\n%s\n' "$t" "$names"; }
  done
  return 0
}

print_brief() {      # counts + a few examples; the full list is `topics`
  local out; out="$(shared_topics)"
  [ -n "$out" ] || { echo "   (the robot shares nothing right now)"; return 1; }
  local total; total="$(printf '%s\n' "$out" | wc -l)"
  if [ "$total" -le 25 ]; then print_topics; return $?; fi
  printf '\n   The robot shares %s items:\n' "$total"
  for k in "Publisher|topics you can read" "Subscriber|topics you can write to" "Service Server|services" "Action Server|actions"; do
    local kind="${k%%|*}" label="${k#*|}"
    local n; n="$(printf '%s\n' "$out" | awk -F'\t' -v k="$kind" '$1==k' | wc -l)"
    [ "$n" -gt 0 ] && printf '     %4s %s\n' "$n" "$label"
  done
  printf '   (everything on the robot crosses the tunnel: ROS tools here see it as if they ran on the robot)  Full list: bash %s topics\n' "$SELF"
}

# ---------------------------------------------------------------- files other commands rely on
local_distro() {
  if [ -n "$ROS_DISTRO" ]; then echo "$ROS_DISTRO"; return; fi
  for d in jazzy humble iron kilted rolling; do [ -f "/opt/ros/$d/setup.bash" ] && { echo "$d"; return; }; done
  echo "$ROBOT_DISTRO"
}

# Replace a file atomically, and only when its content really changes (a running bash script must never be
# overwritten in place). Returns 0 when it changed, 1 when it was already identical.
put_file() {   # put_file PATH MODE   (new content on stdin)
  local f="$1" mode="$2" t; t="$(mktemp "$f.XXXXXX")" || return 1
  cat > "$t"; chmod "$mode" "$t"
  if [ -f "$f" ] && cmp -s "$t" "$f"; then rm -f "$t"; return 1; fi
  mv -f "$t" "$f"
}

RESTART_NEEDED=0
write_files() {
  put_file "$BASE/cyclone-local.xml" 644 <<'XML' && RESTART_NEEDED=1
<CycloneDDS><Domain>
  <General><Interfaces><NetworkInterface address="127.0.0.1" multicast="true"/></Interfaces></General>
  <Discovery><ParticipantIndex>auto</ParticipantIndex><MaxAutoParticipantIndex>64</MaxAutoParticipantIndex><Peers><Peer Address="127.0.0.1"/></Peers></Discovery>
</Domain></CycloneDDS>
XML
  put_file "$BASE/env.sh" 644 <<EOF
# source this before running rviz2 / ros2 to see the robot's topics (generated by lumi-rviz.sh)
if [ -z "\$ROS_DISTRO" ]; then
  for d in jazzy humble iron kilted rolling; do [ -f /opt/ros/\$d/setup.bash ] && { . /opt/ros/\$d/setup.bash; break; }; done
fi
unset ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE
# On WSL, RViz2 rendering through the Windows GPU driver (Mesa d3d12) can freeze the window / leave it blank.
# Software rendering is plenty for RViz2; remove the next line if you prefer the GPU path.
if grep -qi microsoft /proc/version 2>/dev/null; then export LIBGL_ALWAYS_SOFTWARE=1; fi
# WSLg shows Linux windows through these two variables; a terminal that was opened some other way may lack them.
if [ -d /mnt/wslg ] && [ -z "\$DISPLAY\$WAYLAND_DISPLAY" ]; then export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0; fi
export ROS_DOMAIN_ID=$DOMAIN
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file://$BASE/cyclone-local.xml
EOF
  put_file "$RUN/supervisor.env" 600 <<EOF && RESTART_NEEDED=1
LUMI_BASE='$BASE'
CF='$CF'
BRIDGE='$BRIDGE'
HOST='$HOST'
APP='$APP'
PORT='$PORT'
DOMAIN='$DOMAIN'
ROSDIST='$(local_distro)'
EOF
  put_file "$BASE/supervisor.sh" 755 <<'SUP' && RESTART_NEEDED=1
#!/usr/bin/env bash
# Keeps the Cloudflare tunnel and the Zenoh bridge running. Started by lumi-rviz.sh; stop with `lumi-rviz.sh stop`.
. "$1"
LOGS="$LUMI_BASE/logs"; RUN="$LUMI_BASE/run"
echo $$ > "$RUN/supervisor.pid"
log() { echo "$(date '+%F %T') $*" >> "$LOGS/supervisor.log"; }
# The bridge MUST know the ROS distro (otherwise it assumes 'iron' and mis-reads ros_discovery_info) and
# discover local ROS programs over the loopback interface ONLY (never the LAN). Multicast is forced ON for that interface:
# many machines (a freshly started WSL, the Pi) have a loopback without the MULTICAST flag, and with multicast off the
# bridge cannot see local ROS programs there even though the programs see each other. Loopback multicast works anyway.
export ROS_DISTRO="$ROSDIST" ROS_DOMAIN_ID="$DOMAIN" CYCLONEDDS_URI="file://$LUMI_BASE/cyclone-local.xml"
unset ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE
TOKEN_ARGS=()
if [ -n "$CF_ACCESS_CLIENT_ID" ] && [ -n "$CF_ACCESS_CLIENT_SECRET" ]; then
  TOKEN_ARGS=(--service-token-id "$CF_ACCESS_CLIENT_ID" --service-token-secret "$CF_ACCESS_CLIENT_SECRET")
fi
port_listening() { python3 - "$PORT" <<'PY'
import sys
p = int(sys.argv[1])
for f in ('/proc/net/tcp', '/proc/net/tcp6'):
    try:
        for l in open(f).read().splitlines()[1:]:
            c = l.split()
            if c[3] == '0A' and int(c[1].split(':')[1], 16) == p:
                sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PY
}
signed_in() { [ ${#TOKEN_ARGS[@]} -gt 0 ] || "$CF" access token -app="$APP" >/dev/null 2>&1; }

tunnel_loop() {
  n=0
  while true; do
    if ! signed_in; then log "Cloudflare Access sign-in missing or expired - run: lumi-rviz.sh login"; sleep 30; continue; fi
    t0=$(date +%s)
    "$CF" access tcp --hostname "$HOST" --url "127.0.0.1:$PORT" "${TOKEN_ARGS[@]}" >> "$LOGS/tunnel.log" 2>&1
    ran=$(( $(date +%s) - t0 )); log "tunnel (cloudflared) exited after ${ran}s"
    if [ "$ran" -ge 60 ]; then n=0; else n=$((n + 1)); fi
    sleep $(( n > 6 ? 60 : 2 * n + 3 ))
  done
}
bridge_loop() {
  n=0
  while true; do
    until port_listening; do sleep 2; done
    if ! signed_in; then sleep 30; continue; fi            # do not poke the tunnel while signed out
    t0=$(date +%s)
    "$BRIDGE" client -e "tcp/127.0.0.1:$PORT" --no-multicast-scouting -d "$DOMAIN" > "$LOGS/bridge.log" 2>&1
    ran=$(( $(date +%s) - t0 ))
    log "bridge exited after ${ran}s: $(sed 's/\x1b\[[0-9;]*m//g' "$LOGS/bridge.log" | grep -E 'Unable|Failed|ERR' | tail -1 | cut -c1-200)"
    if [ "$ran" -ge 60 ]; then n=0; else n=$((n + 1)); fi
    sleep $(( n > 6 ? 30 : n + 2 ))
  done
}
tunnel_loop & TL=$!
bridge_loop & BL=$!
cleanup() { for p in $TL $BL; do pkill -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done; rm -f "$RUN/supervisor.pid"; exit 0; }
trap cleanup TERM INT
log "supervisor started (host $HOST, port $PORT, domain $DOMAIN)"
wait
SUP
}

# ---------------------------------------------------------------- sign-in
sign_in() {
  if using_service_token; then ok "Cloudflare Access: using the service token (no browser sign-in needed)"; return 0; fi
  if signed_in; then ok "Cloudflare Access: already signed in"; return 0; fi
  say "Cloudflare Access sign-in (needed about once a day)"
  echo "   Open the link printed below in ANY browser (your laptop is fine) and approve. This window waits."
  "$CF" access login "$APP" || warn "the sign-in did not finish - continuing (fine only if this hostname has no Access policy)"
  signed_in
}

# ---------------------------------------------------------------- commands
do_stop() {
  if sup_running; then
    local p; p="$(cat "$RUN/supervisor.pid")"
    kill "$p" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$p" 2>/dev/null
  fi
  # belt and braces: anything of ours still holding the port / running from our directory
  pkill -f "bash $BASE/supervisor.sh" 2>/dev/null           # its two helper loops, if the supervisor itself died hard
  pkill -f "access tcp --hostname $HOST --url 127.0.0.1:$PORT" 2>/dev/null
  pkill -f "zenoh-bridge-ros2dds client -e tcp/127.0.0.1:$PORT" 2>/dev/null
  rm -f "$RUN/supervisor.pid"
  ok "stopped"
}

ISSUES=0
bad() { warn "$@"; ISSUES=$((ISSUES + 1)); }

# Add / remove the small block in ~/.bashrc that makes a NEW terminal ready for rviz2 (same settings as env.sh).
bashrc_block() {   # bashrc_block add|remove -> prints added|updated|unchanged|removed|none
  python3 - "$1" "$HOME/.bashrc" "$BASE" <<'PY'
import os, re, sys, tempfile
mode, rc, base = sys.argv[1:4]
b, e = '# >>> lumi-rviz >>>', '# <<< lumi-rviz <<<'
block = ('%s (added by lumi-rviz.sh - remove it with: bash lumi-rviz.sh unsetup)\n'
         '[ -f "%s/env.sh" ] && . "%s/env.sh"\n%s\n') % (b, base, base, e)
rc = os.path.realpath(rc)
txt = open(rc, errors='surrogateescape').read() if os.path.exists(rc) else ''
had = b in txt
rest = re.sub(re.escape(b) + r'.*?' + re.escape(e) + r'\n?', '', txt, flags=re.S).rstrip('\n')
if mode == 'remove':
    new = (rest + '\n') if rest else ''
else:
    new = (rest + '\n\n' if rest else '') + block
if new == txt:
    print('unchanged' if mode == 'add' else 'none'); sys.exit(0)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(rc))
with os.fdopen(fd, 'w', errors='surrogateescape') as f:
    f.write(new)
if os.path.exists(rc):
    os.chmod(tmp, os.stat(rc).st_mode & 0o777)
os.replace(tmp, rc)
print('removed' if mode == 'remove' else ('updated' if had else 'added'))
PY
}

setup_shell() {
  if [ ! -f "$HOME/.bashrc" ] && [ "${SHELL##*/}" != bash ]; then
    warn "your shell is not bash: add this line to its startup file yourself:  . $BASE/env.sh"; return
  fi
  case "$(bashrc_block add)" in
    added)   ok "new terminals are ready for rviz2 (added a small block to ~/.bashrc; undo: bash $SELF unsetup)" ;;
    updated) ok "terminal setup in ~/.bashrc refreshed" ;;
    *)       ok "new terminals are already set up for rviz2 (~/.bashrc)" ;;
  esac
}

do_unsetup() {
  case "$(bashrc_block remove)" in
    removed) ok "removed the lumi-rviz block from ~/.bashrc (open terminals keep their settings until they close)" ;;
    *)       ok "nothing to remove - ~/.bashrc has no lumi-rviz block" ;;
  esac
}

# Is ROS 2 + RViz2 + the CycloneDDS middleware here? Only a hint: a headless machine can still use the tunnel.
check_local_ros() {
  local r; r="$( (
    . "$BASE/env.sh" 2>/dev/null
    have ros2 || { echo "noros"; exit 0; }
    ros2 pkg prefix rmw_cyclonedds_cpp >/dev/null 2>&1 || { echo "nocyclone:$ROS_DISTRO"; exit 0; }
    have rviz2 || { echo "norviz:$ROS_DISTRO"; exit 0; }
    echo "ok:$ROS_DISTRO"
  ) )"
  case "$r" in
    ok:*)       ok "ROS 2 ${r#ok:} with RViz2 and CycloneDDS found" ;;
    nocyclone:*) warn "CycloneDDS middleware missing - RViz2/ros2 will not see the robot:  sudo apt install ros-${r#nocyclone:}-rmw-cyclonedds-cpp" ;;
    norviz:*)   warn "RViz2 is not installed here (fine on a headless machine). To get it:  sudo apt install ros-${r#norviz:}-rviz2" ;;
    *)          warn "ROS 2 is not installed here (fine on a headless machine; the tunnel and topic list still work). For RViz2 install ROS 2, rviz2 and rmw-cyclonedds-cpp." ;;
  esac
}

port_holder() {    # "PID<TAB>command line" of our own process that listens on the tunnel port, if any
  local pid; pid="$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  [ -n "$pid" ] && printf '%s\t%s\n' "$pid" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
}

bridge_proc() { pgrep -f "zenoh-bridge-ros2dds client -e tcp/127.0.0.1:$PORT" >/dev/null 2>&1; }

start_background() {
  # Leftovers of an earlier run that no supervisor looks after any more (incl. orphaned helper loops that would respawn them).
  pkill -f "bash $BASE/supervisor.sh" 2>/dev/null
  pkill -f "zenoh-bridge-ros2dds client -e tcp/127.0.0.1:$PORT" 2>/dev/null
  if port_listening; then
    local h pid cmd; h="$(port_holder)"; pid="${h%%$'\t'*}"; cmd="${h#*$'\t'}"
    case "$cmd" in
      *"access tcp"*"$HOST"*) kill "$pid" 2>/dev/null; for _ in 1 2 3 4 5 6; do port_listening || break; sleep 0.5; done
                              ok "stopped an old tunnel to this host that nothing was looking after" ;;
      *) die "port $PORT is already used by something else ${cmd:+($cmd)}. Free it, or run this script with LUMI_HOME=... after changing the port in the panel." ;;
    esac
    port_listening && die "port $PORT is still busy after stopping the old tunnel - stop it by hand and run this again"
  fi
  [ "$(stat -c %s "$LOGS/tunnel.log" 2>/dev/null || echo 0)" -gt 5000000 ] && : > "$LOGS/tunnel.log"
  : > "$LOGS/bridge.log"            # so an old run's "connected" line cannot fool the wait below
  say "Starting the tunnel and the bridge in the background"
  if have setsid; then
    setsid nohup bash "$BASE/supervisor.sh" "$RUN/supervisor.env" >> "$LOGS/supervisor.log" 2>&1 < /dev/null &
  else
    nohup bash "$BASE/supervisor.sh" "$RUN/supervisor.env" >> "$LOGS/supervisor.log" 2>&1 < /dev/null &
  fi
  disown 2>/dev/null
  for _ in $(seq 1 20); do sup_running && break; sleep 0.5; done
  sup_running || die "the background process did not start - see: bash $SELF logs supervisor"
}

print_next_steps() {
  echo
  if [ "$ISSUES" -eq 0 ]; then echo "${G}${B}Everything is set up and checked.${N}"; else echo "${Y}${B}Set up, but $ISSUES check(s) need attention (see the lines marked !! above).${N}"; fi
  echo
  echo "   ${B}RViz2:${N}   open a NEW terminal and run   rviz2        (this terminal: first run  source $BASE/env.sh)"
  echo "            or from anywhere:  bash $SELF rviz        (add the displays you want, or open your own file with -d <file>)"
  echo "   ${B}After a disconnect / restart:${N} run this script again. It only re-checks and repairs what is missing - nothing that works is touched."
  echo "   Stop the background tasks:  bash $SELF stop         Check the topics:  bash $SELF topics         Data test:  bash $SELF test"
  echo "   Something wrong?            bash $SELF diag"
}

do_start() {
  ISSUES=0
  say "Checking this machine"
  ensure_tools; ensure_cloudflared; ensure_bridge        # each one installs itself only when it is missing
  ok "cloudflared and the Zenoh bridge are installed"
  [ "$HOST" = "zenoh.example.com" ] && die "Pass the tunnel hostname once:  bash $SELF your-hostname.example.com"
  echo "$HOST" > "$BASE/host"
  sign_in || bad "not signed in - the tunnel will not connect until you finish:  bash $SELF login"
  RESTART_NEEDED=0; write_files
  check_local_ros
  setup_shell

  # ---- the background tunnel + bridge: keep what works, repair what does not
  if sup_running && [ "$RESTART_NEEDED" = 1 ]; then
    say "The settings changed - restarting the background tasks once"; do_stop
  fi
  if sup_running; then
    for _ in $(seq 1 20); do port_listening && bridge_proc && break; sleep 1; done      # they restart themselves after a hiccup
    if port_listening && bridge_proc; then ok "tunnel and bridge are already running (left untouched)"
    else warn "the background tasks are running but unhealthy - restarting them"; do_stop; fi
  fi
  sup_running || start_background

  # ---- is the robot there?
  if ! bridge_connected; then
    printf '   waiting for the robot'
    for _ in $(seq 1 45); do bridge_connected && break; printf '.'; sleep 1; done
    echo
  fi
  if bridge_connected; then
    sleep 2      # the robot announces what it shares right after the bridges meet
    ok "connected to the robot through $HOST"
    print_brief || true
    if [ -n "${LUMI_NO_TEST:-}${NO_TEST:-}" ]; then echo "   (data check skipped)"
    else
      say "Checking that the data really arrives"
      ( do_test ) || ISSUES=$((ISSUES + 1))
    fi
  else
    bad "the tunnel is up but the robot is not reachable yet (is its system running? Lumi panel -> RViz bridge). It keeps trying in the background; re-run this script later, or:  bash $SELF diag"
  fi
  print_next_steps
  [ "$ISSUES" -eq 0 ]
}

do_status() {
  local rc=0
  echo "${B}Lumi remote bridge${N}  host: $HOST   port: 127.0.0.1:$PORT   domain: $DOMAIN"
  if sup_running; then ok "background supervisor running (pid $(cat "$RUN/supervisor.pid"))"; else warn "not running - start it with: bash $SELF"; rc=1; fi
  if port_listening; then ok "tunnel open on 127.0.0.1:$PORT"; else warn "tunnel not open (cloudflared not running or not signed in)"; rc=1; fi
  pgrep -f "zenoh-bridge-ros2dds client -e tcp/127.0.0.1:$PORT" >/dev/null 2>&1 && ok "bridge running" || { warn "bridge not running"; rc=1; }
  if using_service_token; then ok "Access: service token"; elif [ -n "${CF:-}" ]; then
    "$CF" access token -app="$APP" >/dev/null 2>&1 && ok "Access: signed in" || { warn "Access: NOT signed in (or expired) - run: bash $SELF login"; rc=1; }
  fi
  if bridge_connected; then
    local n; n="$(shared_topics | wc -l)"
    if [ "$n" -gt 0 ]; then ok "connected to the robot - it shares $n item(s)  (details: bash $SELF topics)"
    else warn "connected, but the robot shares nothing right now (is its system running? Lumi panel -> Start system)"; rc=1; fi
  else warn "not connected to the robot"; rc=1; fi
  return $rc
}

do_rviz() {
  # Start the tunnel + bridge first if they are not running (e.g. after a reboot / wsl --shutdown).
  if ! sup_running || ! bridge_connected; then
    say "The bridge is not connected yet - starting it first"
    [ "$HOST" = "zenoh.example.com" ] && die "Pass the tunnel hostname once:  bash $SELF your-hostname.example.com"
    sup_running && { warn "it is running but not connected to the robot yet (bash $SELF diag) - opening rviz2 anyway"; } || do_start
  fi
  [ -f "$BASE/env.sh" ] || die "run the setup first:  bash $SELF your-hostname.example.com"
  # shellcheck disable=SC1091
  . "$BASE/env.sh"
  have rviz2 || die "rviz2 was not found - install ROS 2 and RViz2 (e.g. sudo apt install ros-${ROS_DISTRO:-<distro>}-rviz2 ros-${ROS_DISTRO:-<distro>}-rmw-cyclonedds-cpp)"
  # Terminals opened some other way (ssh, a service) can lack the display variables. WSLg uses these two.
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ -d /mnt/wslg ]; then export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0; fi
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || die "no display here (headless machine). RViz2 needs a screen; use 'bash $SELF test' to check the data instead"
  say "Opening rviz2 (DDS domain $DOMAIN, the robot's topics are visible here)"
  exec rviz2 "${RVIZ_ARGS[@]}"
}

do_topics() {
  bridge_connected || { warn "the bridge is not connected to the robot (bash $SELF status / diag)"; exit 1; }
  echo "${B}What the robot shares${N}"; print_topics; local rc=$?; echo; exit $rc
}

do_test() {
  bridge_connected || die "the bridge is not connected to the robot yet (bash $SELF diag)"
  local pubs; pubs="$(shared_topics | awk -F'\t' '$1=="Publisher"{print $2}')"
  [ -n "$pubs" ] || die "the robot shares no topics right now - is the robot system running?"
  ok "bridge level: $(printf '%s\n' "$pubs" | wc -l) topic(s) announced by the robot"
  if ! { have ros2 || ls /opt/ros/*/setup.bash >/dev/null 2>&1; }; then
    warn "ROS 2 is not installed here, so I cannot check the DATA. The topics are reachable (bridge level) - install ROS 2 + rmw_cyclonedds_cpp to test data."
    return 0
  fi
  # shellcheck disable=SC1091
  . "$BASE/env.sh" 2>/dev/null
  have ros2 || die "ros2 not found after sourcing ROS."
  ros2 pkg prefix rmw_cyclonedds_cpp >/dev/null 2>&1 || warn "rmw_cyclonedds_cpp is missing: sudo apt install ros-$ROS_DISTRO-rmw-cyclonedds-cpp  (without it RViz2/ros2 will not see the robot)"
  echo "   ROS $ROS_DISTRO, domain $ROS_DOMAIN_ID, RMW $RMW_IMPLEMENTATION"
  # One in-process probe for everything (no `ros2` command-line tools: their background daemon goes stale easily and then
  # fails with "!rclpy.ok()"). It waits for discovery, subscribes to each topic with a QoS that matches its publishers
  # (the way `ros2 topic echo` does), and for /scan also tries both QoS settings RViz2 may use plus the transform tree.
  local cand=""
  # Only topics that publish continuously: /amcl_pose, /plan and the costmap updates are event-driven (they only speak
  # when the robot localises / navigates), so silence there is normal and not a failure.
  for t in /scan /odom /tf /tf_static /map; do printf '%s\n' "$pubs" | grep -qx "$t" && cand="$cand $t"; done
  [ -n "$cand" ] || cand="$(printf '%s\n' "$pubs" | head -3 | tr '\n' ' ')"      # not our usual robot: test whatever it shares
  local fails=0
  echo "   checking:$cand  (about 20 s) ..."
  LUMI_SELF="$SELF" timeout 120 python3 - $cand <<'PY'
import os, sys, time, rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, qos_profile_sensor_data
try:
    from rclpy.event_handler import SubscriptionEventCallbacks
except ImportError:
    from rclpy.qos_event import SubscriptionEventCallbacks
from rclpy.time import Time
from rosidl_runtime_py.utilities import get_message
import tf2_ros

cands = sys.argv[1:]
rclpy.init()
n = Node('lumi_check')
buf = tf2_ros.Buffer()
tf2_ros.TransformListener(buf, n)

# 1. discovery: wait until ROS here can see the topics the bridge announced
types, t0 = {}, time.time()
while time.time() - t0 < 12 and len(types) < len(cands):
    rclpy.spin_once(n, timeout_sec=0.2)
    known = dict(n.get_topic_names_and_types())
    for t in cands:
        if t in known and t not in types:
            types[t] = known[t][0]
problems = 0
for t in cands:
    if t not in types:
        print('    !! %s is announced by the bridge but ROS here cannot see it (a local discovery problem)' % t)
        problems += 1

# 2. subscribe like `ros2 topic echo` would
got, bad, frame, latched_topics = {}, {}, [], set()
def auto_qos(t):
    pubs = n.get_publishers_info_by_topic(t)
    best = any(p.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT for p in pubs)
    latched = bool(pubs) and all(p.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for p in pubs)
    if latched:
        latched_topics.add(t)
    return QoSProfile(depth=100 if t.startswith('/tf') else 10,
                      reliability=ReliabilityPolicy.BEST_EFFORT if best else ReliabilityPolicy.RELIABLE,
                      durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
def sub(label, topic, msg_type, qos):
    got[label], bad[label] = 0, 0
    def on(m, l=label):
        got[l] += 1
        if hasattr(m, 'header') and getattr(m.header, 'frame_id', '') and topic == '/scan' and not frame:
            frame.append(m.header.frame_id)
    cb = SubscriptionEventCallbacks(incompatible_qos=lambda e, l=label: bad.__setitem__(l, bad[l] + 1))
    n.create_subscription(msg_type, topic, on, qos, event_callbacks=cb)
for t, ty in types.items():
    if t == '/map':
        continue              # checked below the way RViz2's Map display subscribes (transient local)
    sub(t, t, get_message(ty), auto_qos(t))
if '/scan' in types:
    from sensor_msgs.msg import LaserScan
    sub('/scan [RViz2 "Reliable"]', '/scan', LaserScan, QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE))
    sub('/scan [RViz2 "Best Effort"]', '/scan', LaserScan, qos_profile_sensor_data)

MAPLABEL = '/map [RViz2 Map display]'
if '/map' in types:
    from nav_msgs.msg import OccupancyGrid
    latched_topics.add(MAPLABEL)
    sub(MAPLABEL, '/map', OccupancyGrid, QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))

# 3. listen
t1 = time.time()
while time.time() - t1 < 15:
    rclpy.spin_once(n, timeout_sec=0.1)
for label in got:
    ok = got[label] > 0
    if not ok and label == MAPLABEL and bad[label]:
        print('    !!  %s: incompatible - this PC bridge offers /map as VOLATILE (stale state after reconnects) while the robot\'s map is'
              ' latched, so the Map display would stay empty. Fix:  bash %s restart   (then reopen RViz2)' % (label, os.environ.get('LUMI_SELF', 'lumi-rviz.sh')))
        problems += 1
        continue
    if not ok and label in latched_topics and not bad[label]:
        # A latched topic (e.g. /map from the map server) is sent ONCE and replayed to late joiners; nothing more is
        # coming, so silence is not proof of a broken tunnel. Say what it means instead of failing the whole check.
        print('    ~~  %s: no sample in 15 s - a latched topic: the robot sends it once and replays it to new listeners,'
              ' which can miss. If RViz shows nothing for it, switch that display off and on again.' % label)
        continue
    print('    %s %s: %s%s' % ('ok ' if ok else '!! ', label, ('%d message(s) in 15 s' % got[label]) if ok else 'no data in 15 s',
                             '  (incompatible QoS reported)' if bad[label] else ''))
    problems += 0 if ok else 1

# 4. what RViz2 needs to place the scan: a transform from its frame to a fixed frame
if frame:
    f, okf = frame[0], []
    for fixed in ('map', 'odom', 'base_link'):
        r = buf.can_transform(fixed, f, Time())
        print("    %s TF %s -> %s %s" % ('ok ' if r else '!! ', fixed, f, 'resolves' if r else 'does NOT resolve'))
        if r:
            okf.append(fixed)
    if not okf:
        print('    !! no fixed frame reaches the scan, so RViz2 would draw nothing for it. /tf and /tf_static must arrive and the robot needs a map/odom frame.')
        problems += 1
    elif 'map' not in okf:
        print("    -> the robot has no 'map' frame yet (not localised): in RViz2 set Fixed Frame to '%s'." % okf[0])
sys.exit(1 if problems else 0)
PY
  [ $? -eq 0 ] || fails=$((fails + 1))
  # A SERVICE round trip (read-only). Node NAMES are not forwarded by the Zenoh bridge (ros2 node list shows just
  # the bridge), but topics, services and actions are.
  local allsh svc; allsh="$(shared_topics)"; svc="$(printf '%s\n' "$allsh" | awk -F'\t' '$1=="Service Server" && $2 ~ /\/get_parameters$/ {print $2}' | head -1)"
  if [ -n "$svc" ]; then
    # capture first, then look (with `pipefail`, a grep -q that quits early makes the sender die of SIGPIPE = a false failure)
    local reply; reply="$(timeout 50 ros2 service call "$svc" rcl_interfaces/srv/GetParameters "{names: ['use_sim_time']}" 2>/dev/null)"
    if printf '%s' "$reply" | grep -q "values="; then
      ok "service call works ($svc answered)"
    else warn "service $svc is announced but gave no answer within 50 s"; fails=$((fails + 1)); fi
  else
    echo "   (the robot offers no get_parameters service right now, so no service round trip was tried)"
  fi
  [ "$fails" -eq 0 ] && ok "all checks passed" || { warn "$fails check(s) failed - send me the output of: bash $SELF diag"; return 1; }
}

do_logs() {
  local w="${LOGWHICH:-bridge}"
  [ "$w" = start ] && w=bridge
  local f="$LOGS/$w.log"; [ -f "$f" ] || die "no such log: $f"
  sed 's/\x1b\[[0-9;]*m//g' "$f" | redact | tail -n 80
}

do_login() {
  ensure_tools; ensure_cloudflared
  using_service_token && { ok "using the service token - nothing to sign in"; return 0; }
  "$CF" access login "$APP" || die "sign-in failed"
  ok "signed in"; sup_running && ok "the background tunnel picks the new sign-in up automatically"
}

do_update() {
  ensure_tools
  local was=0; sup_running && was=1; do_stop
  rm -f "$BIN/cloudflared" "$BIN/zenoh-bridge-ros2dds"
  ensure_cloudflared; ensure_bridge
  [ "$was" -eq 1 ] && do_start
}

do_diag() {
  echo "===== lumi-rviz diag  $(date '+%F %T %Z') ====="
  echo "host=$HOST port=$PORT domain=$DOMAIN base=$BASE"
  echo "kernel: $(uname -srm)   user: $(id -un)   shell: $BASH_VERSION"
  [ -r /etc/os-release ] && . /etc/os-release && echo "os: $PRETTY_NAME"
  echo "python3: $(python3 --version 2>&1)   curl: $(have curl && echo yes || echo no)   wget: $(have wget && echo yes || echo no)"
  echo
  echo "--- binaries"
  local cfb="$BIN/cloudflared"; [ -x "$cfb" ] || cfb="$(command -v cloudflared)"
  echo "cloudflared: ${cfb:-MISSING}  $([ -x "$cfb" ] && "$cfb" --version 2>&1 | head -1)"
  local zb="$BIN/zenoh-bridge-ros2dds"; [ -x "$zb" ] || zb="$(command -v zenoh-bridge-ros2dds)"
  echo "zenoh bridge: ${zb:-MISSING}  $([ -x "$zb" ] && "$zb" --version 2>&1 | grep -v INFO | head -1)"
  echo
  echo "--- processes"
  sup_running && echo "supervisor: running (pid $(cat "$RUN/supervisor.pid"))" || echo "supervisor: NOT running"
  pgrep -af "access tcp --hostname $HOST" | cut -c1-140 | redact | grep . || echo "tunnel process: not found by name (see the port check below)"
  pgrep -af "zenoh-bridge-ros2dds client" | cut -c1-140 | grep . || echo "bridge: not running"
  echo "port $PORT listening: $(port_listening && echo yes || echo NO)"
  echo
  echo "--- Cloudflare Access"
  if using_service_token; then echo "service token in use"; elif [ -x "$cfb" ]; then "$cfb" access token -app="$APP" >/dev/null 2>&1 && echo "signed in (token present)" || echo "NOT signed in / expired"; fi
  echo "HTTPS reachability of $HOST:"
  if have curl; then curl -sS -m 10 -o /dev/null -w "   HTTP %{http_code}  (302 to cloudflareaccess.com = Access is in front, good;  530/1033 = tunnel down;  000 = no connection)\n" -I "$APP" 2>&1 | tail -1; fi
  echo
  echo "--- robot connection"
  bridge_connected && echo "bridge connected to the robot: yes" || echo "bridge connected to the robot: NO"
  echo "shared items: $(shared_topics | wc -l)"; shared_topics | head -40 | sed 's/^/   /'
  echo
  echo "--- ROS on this machine"
  if have ros2 || ls /opt/ros/*/setup.bash >/dev/null 2>&1; then
    ( . "$BASE/env.sh" 2>/dev/null; echo "distro=$ROS_DISTRO domain=$ROS_DOMAIN_ID rmw=$RMW_IMPLEMENTATION"
      ros2 pkg prefix rmw_cyclonedds_cpp >/dev/null 2>&1 && echo "rmw_cyclonedds_cpp: installed" || echo "rmw_cyclonedds_cpp: MISSING (needed)"
      have rviz2 && echo "rviz2: installed" || echo "rviz2: not installed"
      echo "ros2 topic list (no daemon):"; timeout 20 ros2 topic list --no-daemon 2>&1 | head -30 | sed 's/^/   /' )
  else echo "ROS 2 not installed (fine for bridge-level checks)"; fi
  echo
  for f in supervisor tunnel bridge; do
    echo "--- last lines of $f.log"
    [ -f "$LOGS/$f.log" ] && sed 's/\x1b\[[0-9;]*m//g' "$LOGS/$f.log" | grep -vE "ros_discovery_info|Discovered ROS" | redact | tail -n 12 | cut -c1-220 | sed 's/^/   /' || echo "   (none)"
  done
  echo "===== end of diag (secrets are redacted) ====="
}

case "$CMD" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  ensure_tools; find_cloudflared; do_status ;;
  topics)  do_topics ;;
  test)    do_test ;;
  diag)    do_diag ;;
  rviz)    ensure_tools; find_cloudflared; do_rviz ;;
  unsetup) do_unsetup ;;
  logs)    do_logs ;;
  login)   do_login ;;
  update)  do_update ;;
esac
