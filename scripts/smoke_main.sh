#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# Night Mode smoke test entrypoint.

log()   { printf '[INFO] %s\n' "$*"; }
warn()  { printf '[WARN] %s\n' "$*" >&2; }
fail()  { printf '[FAIL] %s\n' "$*" >&2; EXIT_CODE=2; }

EXIT_CODE=0
WARNINGS=()
declare -a runner_env=()

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

SMOKE_TRIM_CONFIG="${SMOKE_TRIM_CONFIG:-1}"
SMOKE_SEED_CAP="${SMOKE_SEED_CAP:-10}"
# If user set a seed cap but not an FB cap, mirror it for FB passes to keep smoke fast.
if [[ -n "${SMOKE_SEED_CAP:-}" && -z "${FB_PASS_CAP:-}" ]]; then
  export FB_PASS_CAP="$SMOKE_SEED_CAP"
fi

TRIM_OK=0
TRIM_CONFIG="$CONFIG"
TRIM_MSG="trim not attempted"
TRIM_CSV=""
TRIM_WARN=""

if [[ "$SMOKE_TRIM_CONFIG" != "0" ]]; then
  log "Attempting smoke trim (seed cap=$SMOKE_SEED_CAP)"
  trim_output=$(python3 - "$CONFIG" "$RUN_ROOT" "$SMOKE_SEED_CAP" <<'PY'
import csv, json, os, sys

config_path, run_root, cap_raw = sys.argv[1:4]

def sh_quote(val: str) -> str:
    return "'" + val.replace("'", "'\"'\"'") + "'"

try:
    cap = int(cap_raw)
    if cap < 0:
        raise ValueError
except Exception:
    print("TRIM_OK=0")
    print("TRIM_MSG='SMOKE_SEED_CAP must be a non-negative integer'")
    sys.exit(0)

trim_config_path = os.path.join(run_root, "smoke_config_trimmed.json")
trim_csv_path = os.path.join(run_root, "smoke_seed_trimmed.csv")
config_dir = os.path.dirname(config_path)

seed_keys = {"seeds", "rows", "artists", "items", "input_rows", "seed_rows", "jobs"}
path_suffixes = (".csv", ".tsv", ".jsonl", ".txt")

state = {
    "warnings": [],
    "csv_count": 0,
    "csv_replaced": False,
    "trimmed_any": False,
}

def resolve_csv_path(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(config_dir, path))
    return path

def trim_csv(src: str) -> bool:
    try:
        with open(src, newline='', encoding='utf-8') as f:
            reader = list(csv.reader(f))
    except Exception as exc:
        state["warnings"].append(f"could not read csv {src}: {exc}")
        return False
    if not reader:
        state["warnings"].append(f"csv {src} is empty")
        return False
    header, *data = reader
    out_rows = data[:cap]
    try:
        with open(trim_csv_path, "w", newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(out_rows)
    except Exception as exc:
        state["warnings"].append(f"failed writing trimmed csv: {exc}")
        return False
    return True

def walk(node):
    if isinstance(node, dict):
        for k, v in list(node.items()):
            k_lower = str(k).lower()

            if k_lower in seed_keys and isinstance(v, list):
                if len(v) > cap:
                    node[k] = v[:cap]
                    state["trimmed_any"] = True
                continue

            if isinstance(v, str) and v.strip().lower().endswith(path_suffixes):
                state["csv_count"] += 1
                if not state["csv_replaced"]:
                    src = resolve_csv_path(v)
                    if trim_csv(src):
                        node[k] = trim_csv_path
                        state["trimmed_any"] = True
                        state["csv_replaced"] = True
                else:
                    state["warnings"].append("multiple CSV paths detected; only first trimmed")
                continue

            if isinstance(v, (dict, list)):
                walk(v)

    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                walk(item)

def collect_candidates(node, path=""):
    candidates = []

    def join_path(base, part):
        if base == "":
            return str(part)
        if isinstance(part, int):
            return f"{base}[{part}]"
        return f"{base}.{part}"

    if isinstance(node, dict):
        for k, v in node.items():
            p = join_path(path, k)
            if isinstance(v, list):
                if len(v) > 10:
                    candidates.append((p, f"list len={len(v)}"))
                for idx, item in enumerate(v):
                    if isinstance(item, (dict, list)):
                        candidates.extend(collect_candidates(item, join_path(p, idx)))
            elif isinstance(v, dict):
                candidates.extend(collect_candidates(v, p))
            elif isinstance(v, str) and v.lower().strip().endswith(path_suffixes):
                candidates.append((p, "path-like string"))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            candidates.extend(collect_candidates(item, join_path(path, idx)))

    return candidates

try:
    with open(config_path, encoding='utf-8') as f:
        data = json.load(f)
except Exception as exc:
    print("TRIM_OK=0")
    print(f"TRIM_MSG={sh_quote(f'failed to load config: {exc}')}")
    sys.exit(0)

# scan candidates before mutation
candidates = collect_candidates(data)
if candidates:
    print(f"Trim candidate paths (top {min(30, len(candidates))}):", file=sys.stderr)
    for path, desc in candidates[:30]:
        print(f" - {path}: {desc}", file=sys.stderr)
else:
    print("Trim candidate paths: none", file=sys.stderr)

# top-level list keys
if isinstance(data, dict):
    for key in list(data.keys()):
        if str(key).lower() in seed_keys and isinstance(data[key], list) and len(data[key]) > cap:
            data[key] = data[key][:cap]
            state["trimmed_any"] = True

walk(data)

if not state["trimmed_any"]:
    msg = "no seed lists or csv paths found; using original config"
    if state["warnings"]:
        msg += " (warnings: " + "; ".join(state["warnings"]) + ")"
    print("TRIM_OK=0")
    print(f"TRIM_MSG={sh_quote(msg)}")
    sys.exit(0)

try:
    with open(trim_config_path, "w", encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
except Exception as exc:
    print("TRIM_OK=0")
    print(f"TRIM_MSG={sh_quote(f'failed to write trimmed config: {exc}')}")
    sys.exit(0)

print("TRIM_OK=1")
print(f"TRIM_CONFIG={sh_quote(trim_config_path)}")
print("TRIM_CSV=" + (sh_quote(trim_csv_path) if state["csv_replaced"] else "''"))
print("TRIM_WARN=" + (sh_quote('; '.join(state["warnings"])) if state["warnings"] else "''"))
print("TRIM_MSG='trim succeeded'")
PY
  )

  eval "$trim_output"

  if [[ "$TRIM_OK" -eq 1 ]]; then
    CONFIG="$TRIM_CONFIG"
    log "Trimming succeeded; using trimmed config: $CONFIG"
    log "Seed cap applied: $SMOKE_SEED_CAP"
    if [[ -n "$TRIM_CSV" ]]; then
      log "Trimmed CSV: $TRIM_CSV"
    fi
    if [[ -n "$TRIM_WARN" ]]; then
      warn "$TRIM_WARN"
    fi
  else
    warn "Trimming failed/unsupported; using original config ($TRIM_MSG)"
  fi
else
  log "Smoke trim skipped (SMOKE_TRIM_CONFIG=0); using original config"
fi

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
env ${runner_env[@]+"${runner_env[@]}"} python3 night_mode_runner.py \
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

# Guard against newline-only raw CSVs
bad_raws=()
while IFS= read -r f; do
  size=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  header=$(head -n 1 "$f" 2>/dev/null || echo "")
  if [[ "$size" -eq 1 || -z "${header//[$' \t\r\n']}" ]]; then
    bad_raws+=("$f:$size")
  fi
done < <(find "$RUN_ROOT" -type f -name "raw.csv" -print)
if [[ ${#bad_raws[@]} -gt 0 ]]; then
  printf '[FAIL] Detected empty-header raw CSV(s):\n' >&2
  for r in "${bad_raws[@]}"; do
    printf '  %s\n' "$r" >&2
  done
  exit 2
fi
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
