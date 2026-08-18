#!/usr/bin/env bash
# The internals-free negative for issue #6, executed rather than asserted.
#
# The restriction is applied to THE HARNESS PROCESS, not to the child:
#   - the CLI's per-user config directory (transcripts included) is replaced by
#     an empty tmpfs in the harness's mount namespace;
#   - every /tmp/claude-http-*.sock is replaced by an empty regular file, so it
#     is no longer connectable;
#   - the child is spawned through a nested bwrap that binds the real config
#     directory and the real sockets back at their real paths, so the CHILD
#     keeps normal access to all three.
#
# Usage: i01_internals_free_negative.sh <scratch-dir> <path-to-i01_supervisor_probe.py>
set -euo pipefail

SCR=${1:?scratch dir}
PROBE=${2:?probe script}
CFG=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
OUT=${I01_OUT:-$SCR/results}

ALIAS_CFG=$SCR/.alias-cfg
ALIAS_SOCK=$SCR/.alias-sock
EMPTY=$SCR/.empty-deny
mkdir -p "$ALIAS_CFG" "$ALIAS_SOCK" "$OUT"
: > "$EMPTY"

outer=(bwrap --dev-bind / / --bind "$CFG" "$ALIAS_CFG" --tmpfs "$CFG")
inner=(bwrap --dev-bind / / --bind "$ALIAS_CFG" "$CFG")
deny=("$CFG")

shopt -s nullglob
for s in /tmp/claude-http-*.sock; do
  [ -S "$s" ] || continue
  b=$ALIAS_SOCK/$(basename "$s")
  : > "$b"
  outer+=(--bind "$s" "$b" --bind "$EMPTY" "$s")
  inner+=(--bind "$b" "$s")
  deny+=("$s")
done
shopt -u nullglob

echo "### deny paths (applied to the harness only): ${deny[*]}"
echo "### child wrapper: ${inner[*]}"

echo
echo "=== A. control: harness unrestricted ==="
( cd "$SCR" && I01_OUT="$OUT" python3 "$PROBE" scenario --cwd "$SCR" \
    --label unrestricted --kill-after 8 --deny-paths "${deny[@]}" )

echo
echo "=== B. negative: harness denied, child not ==="
( cd "$SCR" && I01_OUT="$OUT" I01_CHILD_WRAPPER="${inner[*]}" \
    "${outer[@]}" -- python3 "$PROBE" scenario --cwd "$SCR" \
    --label restricted --kill-after 8 --deny-paths "${deny[@]}" )

echo
echo "=== C. observer (outside the harness): did the restricted run's child write"
echo "===    its transcript to the REAL config directory? ==="
SID=$(python3 - "$OUT/records.jsonl" <<'PY'
import json, sys
sid = None
for line in open(sys.argv[1], encoding="utf-8"):
    r = json.loads(line)
    if r.get("step") == "scenario" and r.get("label") == "restricted":
        sid = r.get("requested_session_id")
print(sid or "")
PY
)
echo "restricted run requested session id: $SID"
if [ -n "$SID" ]; then
  # find exits 0 even when nothing matches, so test the captured output.
  hits=$(find "$CFG/projects" -name "*$SID*" -printf '%p  %s bytes\n' 2>/dev/null | head -5)
  if [ -n "$hits" ]; then
    echo "$hits"
  else
    echo "NO TRANSCRIPT FOUND -- the child did NOT get normal access; this run is void"
    exit 1
  fi
else
  echo "NO restricted-run record found in $OUT/records.jsonl; this run is void"
  exit 1
fi
