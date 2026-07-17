#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${DEX_MANIPULATION_VENV:-${ROOT}/.venv}"
PYTHON="${PYTHON:-python3}"
PROFILE="${1:-all}"

case "${PROFILE}" in
  core|all) ;;
  *) echo "usage: $0 [core|all]" >&2; exit 2 ;;
esac

"${PYTHON}" -m venv --system-site-packages "${VENV}"
PIP="${VENV}/bin/pip"
"${PIP}" install --upgrade "pip==25.1.1" "setuptools==81.0.0" "wheel==0.46.3"
"${PIP}" install --index-url https://download.pytorch.org/whl/cpu "torch==2.7.0"

if [[ "${PROFILE}" == "core" ]]; then
  "${PIP}" install -e "${ROOT}" --no-deps
  "${PIP}" install "numpy==1.26.0" "PyYAML==6.0.2" "safetensors==0.7.0"
  exit 0
fi

"${PIP}" install -e "${ROOT}[manus,linker,pedal,dev]" --no-deps
"${PIP}" install \
  "numpy==1.26.0" \
  "PyYAML==6.0.2" \
  "safetensors==0.7.0" \
  "python-can==4.6.1" \
  "evdev==1.7.1" \
  "pytest==9.0.3" \
  "import-linter==2.3" \
  "dex-retargeting @ git+https://github.com/dexsuite/dex-retargeting.git@3f56141bc8bd2760d5e452e382937269554ebb21"

SDK_DIR="${ROOT}/.vendor/linkerhand-ros-sdk"
SDK_COMMIT="2aa379cd11562d953f8b449561107b58c120676e"
G20_DRIVER="linker_hand_sdk_ros/scripts/LinkerHand/core/can/linker_hand_g20_can.py"
G20_BASE_SHA256="31a4b5c7aa33636467374e62994a6e4dbb4e2017266934036908e439c49e0715"
G20_PATCHED_SHA256="513be964dee481773ad4d346559e59912b3b70c79c920528783648631f9e10b9"
if [[ ! -d "${SDK_DIR}/.git" ]]; then
  git clone https://github.com/linker-bot/linkerhand-ros-sdk.git "${SDK_DIR}"
fi
git -C "${SDK_DIR}" fetch origin
if [[ "$(git -C "${SDK_DIR}" rev-parse HEAD)" != "${SDK_COMMIT}" ]]; then
  git -C "${SDK_DIR}" checkout --detach "${SDK_COMMIT}"
fi
current_driver_sha256="$(sha256sum "${SDK_DIR}/${G20_DRIVER}" | cut -d ' ' -f 1)"
if [[ "${current_driver_sha256}" == "${G20_BASE_SHA256}" ]]; then
  git -C "${SDK_DIR}" apply "${ROOT}/vendor/patches/linkerhand-g20-required.patch"
elif [[ "${current_driver_sha256}" != "${G20_PATCHED_SHA256}" ]]; then
  echo "refusing unknown G20 driver content: ${current_driver_sha256}" >&2
  exit 1
fi
printf '%s  %s\n' \
  "${G20_PATCHED_SHA256}" \
  "${SDK_DIR}/${G20_DRIVER}" \
  | sha256sum -c -

cat <<'EOF'
Bootstrap complete.

For live Manus input, install ROS 2 plus the manus_ros2_msgs workspace on the
host; rclpy and the message package are system/runtime capabilities and are
validated before a ROS subscription is created.
EOF
