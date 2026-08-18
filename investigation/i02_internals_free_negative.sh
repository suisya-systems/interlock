#!/usr/bin/env bash
# The internals-free negative for issue #7, run on the CONVERSATION half.
#
# Issue #6's negative covered the single-shot path. This one drives the whole
# multi-turn cycle -- create a session carrying a codeword, then resume it in a
# fresh process and make the child recall the codeword -- with the restriction
# applied THE RIGHT WAY ROUND:
#
#   - the restriction is on INTERLOCK'S OWN PROCESS (here, the harness): the
#     CLI's per-user config directory (transcripts included) is replaced by an
#     empty tmpfs in the harness's mount namespace, and every
#     /tmp/claude-http-*.sock is replaced by an empty regular file;
#   - the CHILD keeps normal access: it is spawned through a nested bwrap that
#     binds the real config directory and the real sockets back at their real
#     paths. Under C2 the CLI reconstructs the conversation from its own
#     transcript, so denying the child its transcript would deny the provider
#     its documented function and would prove nothing.
#
# Usage: i02_internals_free_negative.sh <scratch-dir> <fixture-cwd> <probe.py>
set -euo pipefail

SCR=$(readlink -f "${1:?scratch dir}")
FIXTURE=$(readlink -f "${2:?fixture cwd}")
PROBE=$(readlink -f "${3:?probe script}")
CFG=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
OUT=${I02_OUT:-$SCR/results}

ALIAS_CFG=$SCR/.alias-cfg
ALIAS_SOCK=$SCR/.alias-sock
EMPTY=$SCR/.empty-deny
mkdir -p "$ALIAS_CFG" "$ALIAS_SOCK" "$OUT"
export I02_OUT="$OUT"
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

# Distinct codewords so the two halves cannot be confused for one another, and
# a fresh session id per half.
UNRESTRICTED_SID=$(python3 -c 'import uuid; print(uuid.uuid4())')
RESTRICTED_SID=$(python3 -c 'import uuid; print(uuid.uuid4())')

cycle() {                      # $1 label, $2 session id, $3 codeword
  local label=$1 sid=$2 word=$3
  python3 "$PROBE" selfcheck --label "$label" --deny-paths "${deny[@]}"
  python3 "$PROBE" turn --cwd "$FIXTURE" --watch-dir "$FIXTURE" \
    --session-id "$sid" --label "$label:create" \
    --prompt "Remember this codeword: $word. Reply with just: stored"
  python3 "$PROBE" turn --cwd "$FIXTURE" --watch-dir "$FIXTURE" \
    --resume "$sid" --label "$label:resume" \
    --prompt "What codeword did I ask you to remember? Reply with just the word."
}

echo
echo "=== A. control: harness unrestricted ==="
cycle "negative:unrestricted" "$UNRESTRICTED_SID" "ZEPHYR-41"
# A negative that cannot fail proves nothing: the transcript check in C is
# satisfied by the FIRST turn, so continuity is asserted here instead.
python3 "$PROBE" verify-cycle --prefix negative:unrestricted --codeword ZEPHYR-41

echo
echo "=== B. negative: harness denied, child not ==="
# %q-quoted, and parsed back with shlex on the Python side, so a path with
# whitespace in it stays one argument.
printf -v inner_q '%q ' "${inner[@]}"
I02_CHILD_WRAPPER="$inner_q" \
  "${outer[@]}" -- bash -c '
    set -euo pipefail
    python3 "$1" selfcheck --label "negative:restricted" --deny-paths "${@:5}"
    python3 "$1" turn --cwd "$2" --watch-dir "$2" --session-id "$3" \
      --label "negative:restricted:create" \
      --prompt "Remember this codeword: $4. Reply with just: stored"
    python3 "$1" turn --cwd "$2" --watch-dir "$2" --resume "$3" \
      --label "negative:restricted:resume" \
      --prompt "What codeword did I ask you to remember? Reply with just the word."
  ' _ "$PROBE" "$FIXTURE" "$RESTRICTED_SID" "MARJORAM-92" "${deny[@]}"
python3 "$PROBE" verify-cycle --prefix negative:restricted --codeword MARJORAM-92

echo
echo "=== C. observer (outside the harness): did the restricted run's child write"
echo "===    its transcript to the REAL config directory? ==="
echo "restricted session id: $RESTRICTED_SID"
hits=$(find "$CFG/projects" -name "*$RESTRICTED_SID*" -printf '%p  %s bytes\n' 2>/dev/null | head -5)
if [ -n "$hits" ]; then
  echo "$hits"
else
  echo "NO TRANSCRIPT FOUND -- the child did NOT get normal access; this run is void"
  exit 1
fi
echo "unrestricted session id: $UNRESTRICTED_SID"
find "$CFG/projects" -name "*$UNRESTRICTED_SID*" -printf '%p  %s bytes\n' 2>/dev/null | head -5
