#!/bin/sh
set -eu
if ! command -v claude >/dev/null 2>&1; then
  npm install -g --prefix "$HOME/.npm-global" --no-fund --no-audit @anthropic-ai/claude-code
  PATH="$HOME/.npm-global/bin:$PATH"
  export PATH
fi
exec claude -p --dangerously-skip-permissions ${QA_MODEL:+--model "$QA_MODEL"} < "$QA_BRIEF_PATH"
