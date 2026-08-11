#!/usr/bin/env bash
# Start and supervise the authorized OpenXR + LinkerHand + Hitbot live console.

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TELEOP_ROOT="${DEX_TELEOP_ROOT:-/home/user/dex_teleop}"
UI_PYTHON="$REPO_ROOT/.venv/bin/python"
CAMERA_PYTHON="${DEX_CAMERA_PYTHON:-/home/user/miniconda3/bin/python}"
HITBOT_PYTHON="$REPO_ROOT/.venv-hitbot/bin/python"
VR_PYTHON="${DEX_VR_PYTHON:-/home/user/miniconda3/envs/dexmachina/bin/python}"
UI_PORT="${DEX_UI_PORT:-8765}"
UI_URL="http://127.0.0.1:${UI_PORT}"
VR_PORT="${DEX_VR_TELEMETRY_PORT:-8770}"
VR_ARM_PORT="${DEX_VR_ARM_PORT:-8771}"
ARM_PORT="${DEX_ARM_TELEMETRY_PORT:-8780}"
ARM_HOLD_PORT="${DEX_ARM_HOLD_PORT:-8781}"
CAN_INTERFACE="can0"
HITBOT_CONNECTION="dex-hitbot"
HITBOT_INTERFACE="enp129s0"
HITBOT_ADDRESS="192.168.58.2"
FLATPAK_BIN="${DEX_FLATPAK_BIN:-flatpak}"
WIVRN_APP_ID="io.github.wivrn.wivrn"
WIVRN_REF="${WIVRN_APP_ID}//stable"

OPEN_BROWSER=1
START_HITBOT=1
START_WIVRN=1
DRY_RUN=0
ENABLE_RL_SWITCH=0
UI_PID=""
HITBOT_PID=""
WIVRN_PID=""
UI_GROUPED=0
HITBOT_GROUPED=0
WIVRN_GROUPED=0
WIVRN_OWNED=0
CLEANING_UP=0

usage() {
  cat <<'EOF'
Usage: tools/start_live_ui.sh [options]

Starts the validated live stack with a synthetic policy in RL_SHADOW. The
operator may request RL only after the Hitbot hold controller is connected.

Options:
  --no-browser  Do not open Chrome automatically.
  --no-hitbot   Start OpenXR + LinkerHand UI without the Hitbot controller.
  --no-wivrn    Do not launch io.github.wivrn.wivrn; require it externally.
  --dry-run     Run preflight checks without starting hardware processes.
  --enable-rl-switch  Enable the gated real-arm hold/RL transition path.
  -h, --help    Show this help.

Press Ctrl-C in this terminal for ordered shutdown:
monitoring: Hitbot controller -> UI SAFE STOP -> OpenXR bridge -> WiVRn;
RL enabled: UI hand-back/re-anchor -> Hitbot controller -> OpenXR bridge -> WiVRn.
EOF
}

log() {
  printf '[live-ui] %s\n' "$*"
}

# The startup poll and the final assertion must test one condition. Polling a
# weaker one lets startup fail the instant the first arm cycle lands, before the
# console's 0.5s hold probe has had a chance to publish `switchable`.
hitbot_startup_ready() {
  curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
    | "$UI_PYTHON" -c '
import json, sys
s = json.load(sys.stdin)["sources"]
h = s.get("hitbot", {})
p = h.get("payload", {})
r = s.get("runtime", {}).get("payload", {})
switch_enabled = sys.argv[1] == "1"
ok = (
    h.get("health") == "healthy"
    and p.get("connected") is True
    and p.get("ik_ok") is True
    and p.get("servo_ok") is True
    and p.get("hold_state", "TELEOP") == "TELEOP"
    and (
        not switch_enabled
        or (r.get("switchable") is True and not r.get("switch_block_reason"))
    )
)
raise SystemExit(0 if ok else 1)
' "$ENABLE_RL_SWITCH" >/dev/null 2>&1
}

fail_hitbot_startup() {
  local fallback="$1"
  tail -n 80 "$RUN_DIR/hitbot.log" >&2 || true
  if grep -Fq 'robot fault main=2 sub=1' "$RUN_DIR/hitbot.log"; then
    fail "Hitbot reports Axis 1 drive fault (main=2, sub=1), which is non-resettable. Inspect the Axis 1 drive alarm in WebApp/controller and resolve it before retrying; no motion command was sent."
  fi
  if grep -Fq 'robot errcode:14' "$RUN_DIR/hitbot.log"; then
    fail "Hitbot rejected an SDK interface call (errcode 14). Check the controller/WebApp fault status before retrying."
  fi
  if grep -Fq 'ConnectionRefusedError' "$RUN_DIR/hitbot.log"; then
    fail "Hitbot controller 192.168.58.2:8080 refused the SDK connection. Verify controller power, Ethernet link, host route/address, and that port 8080 is enabled before retrying; no arm motion was started."
  fi
  fail "$fallback"
}

fail() {
  printf '[live-ui] ERROR: %s\n' "$*" >&2
  exit 1
}

process_alive() {
  [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null
}

wait_for_exit() {
  local pid="$1"
  local attempts="${2:-50}"
  local index
  for ((index = 0; index < attempts; index += 1)); do
    process_alive "$pid" || return 0
    sleep 0.1
  done
  return 1
}

stop_process() {
  local name="$1"
  local pid="${2:-}"
  local grouped="${3:-0}"
  local target="$pid"
  [[ -n "$pid" ]] || return 0
  process_alive "$pid" || return 0
  if (( grouped == 1 )); then
    target="-$pid"
  fi
  log "Stopping $name (PID $pid)..."
  kill -INT -- "$target" 2>/dev/null || true
  if ! wait_for_exit "$pid" 80; then
    log "$name did not exit after SIGINT; sending SIGTERM."
    kill -TERM -- "$target" 2>/dev/null || true
    wait_for_exit "$pid" 30 || true
  fi
  wait "$pid" 2>/dev/null || true
  if (( grouped == 1 )) && kill -0 -- "-$pid" 2>/dev/null; then
    log "$name left a child in its process group; sending SIGTERM to the group."
    kill -TERM -- "-$pid" 2>/dev/null || true
  fi
}

request_ui_stop() {
  process_alive "$UI_PID" || return 0
  log "Requesting UI SAFE STOP..."
  curl -fsS --max-time 2 -X POST "$UI_URL/api/stop" >/dev/null 2>&1 || true
  for _ in {1..100}; do
    if curl -fsS --max-time 1 "$UI_URL/api/status" 2>/dev/null \
      | "$UI_PYTHON" -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("stopped") is True else 1)' \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  log "UI hand-back/stop did not complete within 10 seconds; continuing bounded shutdown."
}

cleanup() {
  (( CLEANING_UP == 0 )) || return 0
  CLEANING_UP=1
  trap - INT TERM EXIT
  set +e

  # When physical switching is enabled, keep the arm owner alive long enough
  # to acknowledge re-anchor/release. On timeout, stopping it remains bounded.
  if (( ENABLE_RL_SWITCH == 1 )); then
    request_ui_stop
  fi
  stop_process "Hitbot controller" "$HITBOT_PID" "$HITBOT_GROUPED"
  if (( ENABLE_RL_SWITCH == 0 )); then
    request_ui_stop
  fi
  stop_process "live console" "$UI_PID" "$UI_GROUPED"
  if (( WIVRN_OWNED == 1 )); then
    stop_process "WiVRn" "$WIVRN_PID" "$WIVRN_GROUPED"
    "$FLATPAK_BIN" kill "$WIVRN_APP_ID" >/dev/null 2>&1 || true
  fi

  log "Shutdown complete."
}

trap cleanup INT TERM EXIT

while (($#)); do
  case "$1" in
    --no-browser)
      OPEN_BROWSER=0
      ;;
    --no-hitbot)
      START_HITBOT=0
      ;;
    --no-wivrn)
      START_WIVRN=0
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --enable-rl-switch)
      ENABLE_RL_SWITCH=1
      ;;
    -h|--help)
      usage
      trap - INT TERM EXIT
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
  shift
done

for command in awk curl grep ip nmcli ping setsid ss; do
  command -v "$command" >/dev/null 2>&1 || fail "Required command is missing: $command"
done

if (( START_WIVRN == 1 )); then
  command -v "$FLATPAK_BIN" >/dev/null 2>&1 \
    || fail "Flatpak is missing; install it or use --no-wivrn with an externally started OpenXR runtime."
  "$FLATPAK_BIN" info "$WIVRN_REF" >/dev/null 2>&1 \
    || fail "WiVRn stable is not installed. Run: flatpak install flathub $WIVRN_REF"
fi
[[ -x "$UI_PYTHON" ]] || fail "UI Python is missing: $UI_PYTHON"
[[ -x "$CAMERA_PYTHON" ]] || fail "D435 capture Python is missing: $CAMERA_PYTHON"
[[ -x "$VR_PYTHON" ]] || fail "VR Python is missing: $VR_PYTHON"
"$CAMERA_PYTHON" -c 'import cv2, numpy, pyrealsense2' >/dev/null 2>&1 \
  || fail "D435 Python support is missing from $CAMERA_PYTHON. Set DEX_CAMERA_PYTHON to the Python used by the working RealSense capture tool."
if ! grep -RqsE 'idVendor.*8086.*idProduct.*0b07|idProduct.*0b07.*idVendor.*8086' \
  /etc/udev/rules.d /lib/udev/rules.d /usr/lib/udev/rules.d 2>/dev/null; then
  fail "D435 udev permission rule is missing. Run ./tools/install_d435_udev.sh, unplug/reconnect the camera, then retry."
fi
[[ -f "$TELEOP_ROOT/main_new.py" ]] \
  || fail "VR integration entrypoint is missing: $TELEOP_ROOT/main_new.py"
[[ -f "$TELEOP_ROOT/vr_utils/vr_hand_reader.py" ]] \
  || fail "VRHandReader is missing under $TELEOP_ROOT"
"$VR_PYTHON" -c 'import numpy, xr' >/dev/null 2>&1 \
  || fail "OpenXR Python support is missing from $VR_PYTHON (requires numpy and pyopenxr/xr)."
if (( START_HITBOT == 1 )); then
  [[ -x "$HITBOT_PYTHON" ]] || fail "Hitbot Python is missing: $HITBOT_PYTHON"
  [[ -f "$REPO_ROOT/tools/vr_hitbot_controller.py" ]] \
    || fail "OpenXR Hitbot owner is missing under $REPO_ROOT/tools"
fi

if curl -fsS --max-time 1 "$UI_URL/api/status" >/dev/null 2>&1; then
  fail "A console is already running at $UI_URL. Stop it normally before starting another."
fi
if (( START_HITBOT == 1 )) && pgrep -f '[v]r_hitbot_controller.py' >/dev/null 2>&1; then
  fail "A Hitbot controller is already running; a second owner is forbidden."
fi
for udp_port in "$VR_PORT" "$VR_ARM_PORT" "$ARM_PORT" "$ARM_HOLD_PORT"; do
  if ss -H -lun | awk '{print $5}' | grep -Eq "[:.]${udp_port}$"; then
    fail "UDP port $udp_port is already owned; stop the stale telemetry/hold process."
  fi
done

if (( START_HITBOT == 1 )); then
  if ! nmcli -t -f NAME connection show --active | grep -Fxq "$HITBOT_CONNECTION"; then
    log "Activating NetworkManager connection $HITBOT_CONNECTION..."
    (( DRY_RUN == 1 )) || nmcli connection up "$HITBOT_CONNECTION" >/dev/null
  fi
  ip link show "$HITBOT_INTERFACE" >/dev/null 2>&1 \
    || fail "Hitbot interface is missing: $HITBOT_INTERFACE"
fi

if ! ip link show "$CAN_INTERFACE" >/dev/null 2>&1; then
  fail "CAN interface is missing: $CAN_INTERFACE"
fi
if ! ip -details link show "$CAN_INTERFACE" | grep -q 'bitrate 1000000'; then
  if (( DRY_RUN == 1 )); then
    log "CAN bitrate requires configuration to 1000000."
  else
    log "Configuring $CAN_INTERFACE at 1 Mbit/s (sudo may prompt)..."
    sudo ip link set "$CAN_INTERFACE" down
    sudo ip link set "$CAN_INTERFACE" type can bitrate 1000000
  fi
fi
if ! ip link show "$CAN_INTERFACE" | grep -Eq '<([^>]*,)?UP(,|>)'; then
  if (( DRY_RUN == 1 )); then
    log "$CAN_INTERFACE requires link-up."
  else
    log "Bringing up $CAN_INTERFACE (sudo may prompt)..."
    sudo ip link set "$CAN_INTERFACE" up
  fi
fi

if (( DRY_RUN == 1 )); then
  log "Dry-run checks passed. No hardware process was started."
  trap - INT TERM EXIT
  exit 0
fi

if (( START_HITBOT == 1 )); then
  ping -c 1 -W 1 "$HITBOT_ADDRESS" >/dev/null 2>&1 \
    || fail "Hitbot is unreachable at $HITBOT_ADDRESS via $HITBOT_INTERFACE"
fi

printf '\nLIVE HARDWARE STARTUP\n'
printf 'This starts real LinkerHand teleoperation from Quest/WiVRn OpenXR and the Hitbot owner.\n'
printf 'Verify workspace clearance, hardware identity, and staffed E-stop access.\n'
printf 'The policy remains synthetic RL_SHADOW; this launcher never presses F12.\n\n'
read -r -p 'Type CONFIRM to continue: ' confirmation
[[ "$confirmation" == "CONFIRM" ]] || fail "Live startup was not confirmed."
if (( ENABLE_RL_SWITCH == 1 )); then
  printf 'REAL ARM HOLD / RL SWITCH ENABLEMENT\n'
  printf 'This path may freeze the Hitbot arm and transfer LinkerHand control to RL.\n'
  read -r -p 'Type ENABLE RL to continue: ' rl_confirmation
  [[ "$rl_confirmation" == "ENABLE RL" ]] \
    || fail "Real-arm RL switching was not explicitly confirmed."
fi

RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$REPO_ROOT/.artifacts/control-console/live-runs/$RUN_STAMP-$$"
mkdir -p "$RUN_DIR"
log "Logs: $RUN_DIR"

if (( START_WIVRN == 1 )); then
  if "$FLATPAK_BIN" ps --columns=application 2>/dev/null \
    | grep -Fxq "$WIVRN_APP_ID"; then
    log "WiVRn is already running; leaving the existing instance under external ownership."
  else
    log "Starting WiVRn for Quest OpenXR..."
    (
      exec setsid "$FLATPAK_BIN" run "$WIVRN_REF"
    ) >"$RUN_DIR/wivrn.log" 2>&1 &
    WIVRN_PID=$!
    WIVRN_GROUPED=1
    WIVRN_OWNED=1
    for _ in {1..100}; do
      if "$FLATPAK_BIN" ps --columns=application 2>/dev/null \
        | grep -Fxq "$WIVRN_APP_ID"; then
        break
      fi
      process_alive "$WIVRN_PID" || break
      sleep 0.1
    done
    "$FLATPAK_BIN" ps --columns=application 2>/dev/null \
      | grep -Fxq "$WIVRN_APP_ID" \
      || fail "WiVRn did not start; see $RUN_DIR/wivrn.log"
  fi
  log "WiVRn ready. Connect Quest 3S and focus the OpenXR session."
fi

log "Starting OpenXR + LinkerHand live console in RL_SHADOW..."
ARM_SWITCH_ARGS=()
if (( ENABLE_RL_SWITCH == 1 )); then
  ARM_SWITCH_ARGS+=(--enable-real-arm-hold-switch)
fi
(
  cd "$REPO_ROOT"
  exec setsid "$UI_PYTHON" tools/switch_web_demo.py \
    --transport hand \
    --policy synthetic \
    --vr real \
    --arm-telemetry live \
    --camera d435 \
    --camera-python "$CAMERA_PYTHON" \
    --host 127.0.0.1 \
    --port "$UI_PORT" \
    --vr-udp-port "$VR_PORT" \
    --vr-arm-udp-port "$VR_ARM_PORT" \
    --vr-python "$VR_PYTHON" \
    --teleop-root "$TELEOP_ROOT" \
    --arm-udp-port "$ARM_PORT" \
    --arm-hold-port "$ARM_HOLD_PORT" \
    "${ARM_SWITCH_ARGS[@]}"
) >"$RUN_DIR/console.log" 2>&1 &
UI_PID=$!
UI_GROUPED=1

for _ in {1..200}; do
  if curl -fsS --max-time 1 "$UI_URL/api/status" >/dev/null 2>&1; then
    break
  fi
  if ! process_alive "$UI_PID"; then
    tail -n 80 "$RUN_DIR/console.log" >&2 || true
    fail "Live console exited during startup."
  fi
  sleep 0.1
done
curl -fsS --max-time 1 "$UI_URL/api/status" >/dev/null 2>&1 \
  || fail "Live console did not become reachable within 20 seconds."

for _ in {1..1200}; do
  if curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
    | "$UI_PYTHON" -c '
import json, sys
s = json.load(sys.stdin)["sources"]
r = s.get("runtime", {}).get("payload", {})
ok = (
    r.get("state") == "RL_SHADOW"
    and r.get("fault") is None
    and s.get("openxr", {}).get("health") == "healthy"
    and s.get("linker", {}).get("health") == "healthy"
    and s.get("d435", {}).get("health") == "healthy"
    and s.get("linker", {}).get("payload", {}).get("connected") is True
)
raise SystemExit(0 if ok else 1)
' >/dev/null 2>&1; then
    break
  fi
  if ! process_alive "$UI_PID"; then
    tail -n 80 "$RUN_DIR/console.log" >&2 || true
    fail "Live console stopped before OpenXR/Linker/D435 became healthy."
  fi
  sleep 0.1
done

if ! curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
  | "$UI_PYTHON" -c '
import json, sys
s = json.load(sys.stdin)["sources"]
r = s.get("runtime", {}).get("payload", {})
raise SystemExit(0 if r.get("state") == "RL_SHADOW" and s.get("openxr", {}).get("health") == "healthy" and s.get("linker", {}).get("health") == "healthy" and s.get("d435", {}).get("health") == "healthy" else 1)
' >/dev/null 2>&1; then
  tail -n 80 "$RUN_DIR/console.log" >&2 || true
  camera_fault="$(curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
    | "$UI_PYTHON" -c 'import json,sys; s=json.load(sys.stdin)["sources"].get("d435",{}); p=s.get("payload",{}); print(p.get("fault") or p.get("source_reason") or s.get("health") or "missing D435 telemetry")' 2>/dev/null || true)"
  openxr_fault="$(curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
    | "$UI_PYTHON" -c 'import json,sys; s=json.load(sys.stdin)["sources"].get("openxr",{}); p=s.get("payload",{}); print(p.get("fault") or p.get("source_reason") or s.get("health") or "missing OpenXR telemetry")' 2>/dev/null || true)"
  fail "OpenXR/Linker/D435 did not reach healthy RL_SHADOW within 120 seconds. Keep the left hand visible until OPENXR is healthy. OpenXR: $openxr_fault; D435: $camera_fault"
fi

if (( START_HITBOT == 1 )); then
  log "Starting the OpenXR/Hitbot controller..."
  (
    cd "$REPO_ROOT"
    exec setsid "$HITBOT_PYTHON" -u tools/vr_hitbot_controller.py \
      --host 127.0.0.1 \
      --vr-port "$VR_ARM_PORT" \
      --telemetry-port "$ARM_PORT" \
      --hold-port "$ARM_HOLD_PORT" \
      --teleop-root "$TELEOP_ROOT"
  ) >"$RUN_DIR/hitbot.log" 2>&1 &
  HITBOT_PID=$!
  HITBOT_GROUPED=1

  # The first SDK cycle follows the first focused OpenXR wrist frame.
  HITBOT_READY=0
  for _ in {1..600}; do
    if hitbot_startup_ready; then
      HITBOT_READY=1
      break
    fi
    if ! process_alive "$HITBOT_PID"; then
      fail_hitbot_startup "Hitbot controller exited during startup."
    fi
    sleep 0.1
  done

  if (( HITBOT_READY == 0 )); then
    hitbot_fault="$(curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
      | "$UI_PYTHON" -c 'import json,sys; s=json.load(sys.stdin)["sources"].get("hitbot",{}); p=s.get("payload",{}); print(p.get("source_reason") or s.get("health") or "missing Hitbot telemetry")' 2>/dev/null || true)"
    switch_fault="$(curl -fsS --max-time 1 "$UI_URL/api/snapshot" 2>/dev/null \
      | "$UI_PYTHON" -c 'import json,sys; r=json.load(sys.stdin)["sources"].get("runtime",{}).get("payload",{}); print(r.get("switch_block_reason") or r.get("switch_gate") or "unknown")' 2>/dev/null || true)"
    fail_hitbot_startup "Hitbot did not reach a healthy TELEOP cycle within 60 seconds. Move the tracked left wrist so the first SDK cycle runs. Hitbot: $hitbot_fault; RL switch: $switch_fault"
  fi
fi

if (( OPEN_BROWSER == 1 )); then
  if command -v google-chrome >/dev/null 2>&1; then
    google-chrome "$UI_URL/" >/dev/null 2>&1 &
  else
    log "Chrome is not installed; open $UI_URL/ manually."
  fi
fi

log "LIVE READY: $UI_URL/"
if (( ENABLE_RL_SWITCH == 1 )); then
  log "State: RL_SHADOW. RL switching is armed only after Hitbot hold-controller verification."
else
  log "State: RL_SHADOW monitoring. RL switching is disabled by the physical-release gate."
fi
log "Press Ctrl-C here for ordered safe shutdown."

while true; do
  process_alive "$UI_PID" || fail "Live console exited; see $RUN_DIR/console.log"
  if (( START_HITBOT == 1 )); then
    process_alive "$HITBOT_PID" || fail "Hitbot controller exited; see $RUN_DIR/hitbot.log"
  fi
  sleep 1
done
