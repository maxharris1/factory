#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOME}/.local/share/factory/venv"
DB="${HOME}/.local/state/factory/orch.db"

mkdir -p "${HOME}/.local/bin" "${HOME}/.config/factory" "$(dirname "$DB")"
uv venv "$VENV"
uv pip install --python "${VENV}/bin/python" -r "${ROOT}/requirements.txt"
cat >"${HOME}/.local/bin/orch" <<EOF
#!/usr/bin/env bash
exec "${VENV}/bin/python" "${ROOT}/bin/orch" "\$@"
EOF
chmod 0755 "${HOME}/.local/bin/orch"
cat >"${HOME}/.config/factory/profile.sh" <<EOF
export ORCH_DB="${DB}"
export PATH="\${HOME}/.local/bin:\${PATH}"
EOF
ORCH_DB="$DB" "${HOME}/.local/bin/orch" init-db
echo "Source ${HOME}/.config/factory/profile.sh in every bot shell."
echo "Then run: orch doctor-runtime"
