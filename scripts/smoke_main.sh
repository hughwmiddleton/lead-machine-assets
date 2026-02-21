#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# Night Mode smoke test entrypoint.

log()   { printf '[INFO] %s\n' "$*"; }
warn()  { printf '[WARN] %s\n' "$*" >&2; }
fail()  { printf '[FAIL] %s\n' "$*" >&2; EXIT_CODE=2; }

EXIT_CODE=0
WARNINGS=()

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_ROOT="${PROJECT_ROOT}/../output_tests"

if ! OUTPUT_ROOT="$(cd "$OUTPUT_ROOT" 2>/dev/null && pwd)"; then
  printf '[FAIL] output_tests directory not found next to repo. Checked: %s\n' "${PROJECT_ROOT}/../output_tests" >&2
  exit 2
fi

RUN_ROOT="${OUTPUT_ROOT}/MAIN_SMOKE_$(date -u +"%Y%m%dT%H%M%SZ")"
mkdir -p "$RUN_ROOT"

cd "$PROJECT_ROOT"

log "Project root: $PROJECT_ROOT"
log "Output root : $OUTPUT_ROOT"
log "Run root    : $RUN_ROOT"

find_config() {
  python3 - "$OUTPUT_ROOT" <<'PY'
import os, sys
root = sys.argv[1]
pref_name = 'overnight_jobs_gui.json'
keywords = {'overnight', 'night', 'jobs', 'gui'}
candidates = []
for dirpath, _, filenames in os.walk(root):
    for fn in filenames:
        path = os.path.join(dirpath, fn)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        candidates.append((mtime, path))

pref = [(m, p) for m, p in candidates if os.path.basename(p) == pref_name]
if pref:
    print(max(pref)[1])
    sys.exit(0)

fallback = [(m, p) for m, p in candidates
            if p.lower().endswith('.json') and any(k in os.path.basename(p).lower() for k in keywords)]
if fallback:
    print(max(fallback)[1])
    sys.exit(0)

sys.stderr.write(f"No suitable config found under {root}\n")
sys.exit(1)
PY
}

CONFIG=""
if ! CONFIG="$(find_config 2>&1)"; then
  fail "Config discovery failed: $CONFIG"
  exit 2
fi

log "Using config: $CONFIG"

check_py() {
  local target="$1"
  local path="$PROJECT_ROOT/$target"
  if [[ -f "$path" ]]; then
    if ! python3 - "$path" <<'PY'
import sys, py_compile
try:
    py_compile.compile(sys.argv[1], doraise=True)
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
PY
    then
      fail "py_compile failed for $target"
    else
      log "py_compile ok: $target"
    fi
  else
    warn "Skipped py_compile (missing): $target"
  fi
}

check_py "night_mode_runner.py"
check_py "night_mode_fb.py"
check_py "cross_directory_enricher.py"

runner_env=()

if [[ -n ${NIGHT_FB_PROFILE_DIR:-} ]]; then
  if [[ -d "$NIGHT_FB_PROFILE_DIR" ]]; then
    log "FB profile enabled: $NIGHT_FB_PROFILE_DIR"
    runner_env+=("NIGHT_FB_PROFILE_DIR=$NIGHT_FB_PROFILE_DIR")
  else
    warn "FB profile path set but missing: $NIGHT_FB_PROFILE_DIR (continuing without profile)"
  fi
fi

if [[ -n ${FB_USERNAME:-} || -n ${FB_PASSWORD:-} ]]; then
  if [[ -n ${FB_USERNAME:-} && -n ${FB_PASSWORD:-} ]]; then
    mask=$(printf '%*s' "${#FB_USERNAME}" '' | tr ' ' '*')
    log "FB credentials provided via env (username masked as: $mask)"
    runner_env+=("FB_USERNAME=$FB_USERNAME" "FB_PASSWORD=$FB_PASSWORD")
  else
    warn "FB credentials partially set; ignoring for safety"
  fi
fi

SMOKE_CONSOLE="$RUN_ROOT/smoke_console.txt"

log "Starting runner (MAX_ROWS=${MAX_ROWS:-10})"

set +e
env "${runner_env[@]}" python3 night_mode_runner.py \
  --config "$CONFIG" \
  --export-mode both \
  --phased \
  --run-root "$RUN_ROOT" \
  --fb-auto-resume \
  --fb-cooldown-seconds "${FB_COOLDOWN:-600}" \
  --fb-max-auto-resume-attempts "${FB_MAX_RESUME:-1}" \
  --fb-max-rows-per-run "${MAX_ROWS:-10}" \
  >"$SMOKE_CONSOLE" 2>&1
RUN_STATUS=$?
set -e

if [[ $RUN_STATUS -ne 0 ]]; then
  fail "Runner exited with status $RUN_STATUS";
fi

CONTACT_LOG=$(find "$RUN_ROOT" -type f -name "contact_log_v2.txt" -print -quit 2>/dev/null || true)
MASTER_RAW=$(find "$RUN_ROOT" -type f -name "master_raw.csv" -print -quit 2>/dev/null || true)

if [[ -z "$MASTER_RAW" ]]; then
  fail "master_raw.csv not found under $RUN_ROOT"
else
  ROWS=$(
    python3 - "$MASTER_RAW" <<'PY'
import csv, sys
path = sys.argv[1]
with open(path, newline='') as f:
    row_count = sum(1 for _ in f)
data_rows = max(0, row_count - 1)
print(data_rows)
PY
  )
  log "master_raw.csv rows (excluding header): $ROWS"
  if [[ "$ROWS" -le 0 ]]; then
    fail "master_raw.csv has no data rows"
  fi
fi

if [[ -n "$CONTACT_LOG" ]]; then
  if grep -qi "Traceback" "$CONTACT_LOG"; then
    fail "Traceback found in contact_log_v2.txt"
  fi
  log "contact_log_v2.txt located: $CONTACT_LOG"
  log "contact_log_v2 tail:"; tail -n 40 "$CONTACT_LOG"
  log "contact_log_v2 summary (candidates_* and success/fail lines):"
  grep -E -i "candidates_(pre|post)_url_gate|success|fail" "$CONTACT_LOG" | tail -n 20 || true
else
  warn "contact_log_v2.txt not found"
fi

if [[ -n "$MASTER_RAW" ]]; then
  log "master_raw.csv header:"; head -n 1 "$MASTER_RAW" || true
fi

if grep -qi "row_index is not defined" "$SMOKE_CONSOLE" ${CONTACT_LOG:+"$CONTACT_LOG"}; then
  fail "Found 'row_index is not defined' in logs"
fi

fb_info=$(
  python3 - "$SMOKE_CONSOLE" "${CONTACT_LOG:-}" <<'PY'
import re, sys
console = sys.argv[1]
contact = sys.argv[2] if len(sys.argv) > 2 else ''
paths = [p for p in [console, contact] if p]
fb_lines = []
metric_vals = []
unauth_tokens = ('unauthenticated', 'missing credentials', 'no auth', 'not authenticated')
for path in paths:
    try:
        with open(path, 'r', errors='ignore') as fh:
            for line in fh:
                if '[Night FB]' in line:
                    fb_lines.append(line)
                m = re.search(r'candidates_pre_url_gate\D+([0-9]+)', line)
                if m:
                    metric_vals.append(int(m.group(1)))
    except FileNotFoundError:
        continue

status = 'no_fb_lines'
if fb_lines:
    if any(tok in ln.lower() for ln in fb_lines for tok in unauth_tokens):
        status = 'skipped'
    else:
        status = 'ran'

metric_present = bool(metric_vals)
metric_nonzero = any(v > 0 for v in metric_vals)

print(f"FB_STATUS={status}")
print(f"FB_METRIC_PRESENT={int(metric_present)}")
print(f"FB_METRIC_NONZERO={int(metric_nonzero)}")
PY
)

eval "$fb_info"

case "$FB_STATUS" in
  ran)
    if [[ "$FB_METRIC_PRESENT" -eq 1 && "$FB_METRIC_NONZERO" -eq 0 ]]; then
      fail "FB ran but candidates_pre_url_gate is zero"
    else
      log "FB activity detected and metrics look ok"
    fi
    ;;
  skipped|no_fb_lines)
    WARNINGS+=("FB skipped due to no auth")
    warn "FB skipped due to no auth"
    ;;
  *)
    warn "FB status indeterminate ($FB_STATUS)"
    ;;
esac

log "Smoke console tail:"; tail -n 40 "$SMOKE_CONSOLE"

if [[ $EXIT_CODE -eq 0 ]]; then
  printf '\nPASS: Night Mode smoke completed. Output: %s\n' "$RUN_ROOT"
  if ((${#WARNINGS[@]})); then
    printf 'Warnings:\n'
    for w in "${WARNINGS[@]}"; do printf ' - %s\n' "$w"; done
  fi
  exit 0
else
  printf '\nFAIL (exit 2): see %s and logs under %s\n' "$SMOKE_CONSOLE" "$RUN_ROOT" >&2
  if ((${#WARNINGS[@]})); then
    printf 'Warnings:\n' >&2
    for w in "${WARNINGS[@]}"; do printf ' - %s\n' "$w" >&2; done
  fi
  exit 2
fi
