#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOME}/.local/share/factory/venv"
DB="${HOME}/.local/state/factory/orch.db"
ORCH_CONFIG="${ORCH_CONFIG:-${ROOT}/orch.yml}"

mkdir -p "${HOME}/.local/bin" "${HOME}/.config/factory" "$(dirname "$DB")"
if [[ "$ROOT" != "${HOME}/factory" ]]; then
    echo "warning: expected the production checkout at ${HOME}/factory; using ${ROOT}" >&2
fi
if git -C "$ROOT" symbolic-ref --quiet --short HEAD >/dev/null 2>&1 &&
    [[ "$(git -C "$ROOT" symbolic-ref --quiet --short HEAD)" != "main" ]]; then
    echo "warning: production installs should use the main branch" >&2
fi
WRAPPER="${HOME}/.local/bin/orch"
if [[ -e "$WRAPPER" ]] && ! grep -q '^# factory-orch-wrapper$' "$WRAPPER"; then
    echo "refusing to overwrite non-factory wrapper at ${WRAPPER}" >&2
    exit 1
fi
if command -v uv >/dev/null 2>&1; then
    UV_VENV_CLEAR=1 uv venv "$VENV"
    uv pip install --python "${VENV}/bin/python" -r "${ROOT}/requirements.txt"
else
    python3 -m venv --clear "$VENV"
    "${VENV}/bin/python" -m pip install -r "${ROOT}/requirements.txt"
fi
cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
# factory-orch-wrapper
exec "${VENV}/bin/python" "${ROOT}/bin/orch" "\$@"
EOF
chmod 0755 "$WRAPPER"
cat >"${HOME}/.config/factory/profile.sh" <<EOF
export ORCH_DB="${DB}"
export PATH="\${HOME}/.local/bin:\${PATH}"
EOF
ORCH_DB="$DB" "$WRAPPER" init-db --orch "$ORCH_CONFIG"
echo "Source ${HOME}/.config/factory/profile.sh in every bot shell."
echo "Then run: orch doctor-runtime"
