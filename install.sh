#!/usr/bin/env bash
#
# Voiccce bootstrap installer.
#
# Detects Python 3.12+ and pipx, installs whatever is missing (via Homebrew
# or a pip --user fallback), installs Voiccce, and prints the next step.
#
# Usage:
#   ./install.sh
#   curl -fsSL https://raw.githubusercontent.com/blackbalancef/voiccce/main/install.sh | bash
#
set -euo pipefail

REPO_URL="https://github.com/blackbalancef/voiccce.git"
CLONE_DIR="${VOICCCE_SRC:-$HOME/voiccce}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=12

# --- pretty output -------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi
info() { printf '%s\n' "${BOLD}==>${RESET} $*"; }
ok()   { printf '%s\n' "  ${GREEN}✓${RESET} $*"; }
warn() { printf '%s\n' "  ${YELLOW}!${RESET} $*"; }
err()  { printf '%s\n' "  ${RED}✗${RESET} $*" >&2; }
run()  { printf '%s\n' "    ${DIM}\$ $*${RESET}"; "$@"; }

py_version() { python3 -c 'import platform; print(platform.python_version())'; }

# --- 0. platform ---------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
  warn "Voiccce targets macOS (voice playback and notifications). Continuing, but some features will not work."
fi

# --- 1. Python 3.12+ -----------------------------------------------------
info "Checking Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+"
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found."
  printf '%s\n' "    Install it: ${BOLD}brew install python@3.12${RESET}  (or https://www.python.org/downloads/)"
  exit 1
fi
if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)"; then
  err "Python $(py_version) is too old; need ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+."
  printf '%s\n' "    Install a newer one: ${BOLD}brew install python@3.12${RESET}  (or https://www.python.org/downloads/)"
  exit 1
fi
ok "Python $(py_version)"

# --- 2. pipx -------------------------------------------------------------
info "Checking pipx"
if command -v pipx >/dev/null 2>&1; then
  PIPX="pipx"
  ok "pipx already installed"
elif command -v brew >/dev/null 2>&1; then
  warn "pipx not found - installing via Homebrew"
  run brew install pipx
  hash -r 2>/dev/null || true
  run pipx ensurepath
  PIPX="pipx"
else
  warn "pipx not found, and Homebrew is missing."
  printf '%s\n' "    Recommended: install Homebrew from ${BOLD}https://brew.sh${RESET}, then re-run this script."
  info "Falling back to a pip --user install of pipx (no Homebrew needed)"
  run python3 -m pip install --user --upgrade pipx
  run python3 -m pipx ensurepath
  PIPX="python3 -m pipx"
fi
hash -r 2>/dev/null || true

# --- 3. locate source ----------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "pyproject.toml" ] && [ -d "agent_voice" ]; then
  SRC="$(pwd)"
  info "Installing from current checkout: ${SRC}"
elif [ -f "${SCRIPT_DIR}/pyproject.toml" ] && [ -d "${SCRIPT_DIR}/agent_voice" ]; then
  SRC="${SCRIPT_DIR}"
  info "Installing from script directory: ${SRC}"
else
  info "Fetching Voiccce into ${CLONE_DIR}"
  if [ -d "${CLONE_DIR}/.git" ]; then
    run git -C "${CLONE_DIR}" pull --ff-only
  else
    run git clone "${REPO_URL}" "${CLONE_DIR}"
  fi
  SRC="${CLONE_DIR}"
fi

# --- 4. install Voiccce --------------------------------------------------
info "Installing Voiccce"
# shellcheck disable=SC2086
run ${PIPX} install --force "${SRC}"
hash -r 2>/dev/null || true

# --- 5. next steps -------------------------------------------------------
echo
if command -v voiccce >/dev/null 2>&1; then
  ok "Installed. Run the setup wizard:"
  printf '%s\n' "    ${BOLD}voiccce setup both${RESET}        ${DIM}# or: voiccce setup both --local  (offline macOS voice)${RESET}"
else
  ok "Installed, but ${BOLD}voiccce${RESET} is not on your PATH yet."
  printf '%s\n' "    Open a new terminal (or run ${BOLD}exec \$SHELL${RESET}), then:"
  printf '%s\n' "    ${BOLD}voiccce setup both${RESET}"
fi
