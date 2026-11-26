#!/usr/bin/env bash
# Portable entry point for one CANoe paired campaign. Run from any WSL directory.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash pi_can_lab/canoe-run-wsl.sh -m METHOD -n COUNT [-x] [options]

Open ThreeChannelCAN.cfg in CANoe, map the three hardware channels, and leave
measurement stopped. This launcher uses the repository's CFG by default and
captures a 30-second receive-only reference baseline for a new campaign.

  -m, --method METHOD             One of the eight 0x366 mutation methods
  -n, --cases COUNT               Cases in this batch (1..20)
  --case-start NUMBER             First case (default: 1)
  -x, --execute                   Probe first, then transmit paired cases
  --probe-only                    Capture baseline and validate passive Rx only
  --canoe-config PATH             CFG actually open in CANoe (WSL path)
  --campaign-dir PATH             Override the fixed method campaign (WSL path)
  --reference-baseline-log PATH   Use an existing BLF/ASC pair instead of capture

Each method uses the fixed campaign path pi_can_lab/logs/canoe_paired/METHOD.
To resume, use the same method with --case-start; no campaign path is needed.
Use --campaign-dir only to start a separate campaign for that method.
Without -x, a fresh campaign captures the passive reference baseline and
previews the plan but does not transmit. Every -x call additionally runs a
6-second passive probe before paired transmission.
EOF
}

fail() {
    echo "[ERROR] $*" >&2
    exit 2
}

need_value() {
    (($# >= 2)) || fail "Missing value after $1"
}

command -v wslpath >/dev/null 2>&1 || fail "This launcher must run inside WSL."

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$script_dir/.." && pwd)"
cfg="$repo_dir/pi_can_lab/canoe/config/ThreeChannelCAN.cfg"
campaign=""
reference=""
method=""
count=""
case_start=1
execute=false
probe_only=false
forward=()

while (($#)); do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -m|--method)
            need_value "$@"
            method="$2"
            shift 2
            ;;
        -m=*|--method=*)
            method="${1#*=}"
            shift
            ;;
        -n|--cases)
            need_value "$@"
            count="$2"
            shift 2
            ;;
        -n=*|--cases=*)
            count="${1#*=}"
            shift
            ;;
        --canoe-config)
            need_value "$@"
            cfg="$2"
            shift 2
            ;;
        --canoe-config=*)
            cfg="${1#*=}"
            shift
            ;;
        --campaign-dir)
            need_value "$@"
            campaign="$2"
            shift 2
            ;;
        --campaign-dir=*)
            campaign="${1#*=}"
            shift
            ;;
        --reference-baseline-log)
            need_value "$@"
            reference="$2"
            shift 2
            ;;
        --reference-baseline-log=*)
            reference="${1#*=}"
            shift
            ;;
        --case-start)
            need_value "$@"
            case_start="$2"
            forward+=("$1" "$2")
            shift 2
            ;;
        --case-start=*)
            case_start="${1#*=}"
            forward+=("$1")
            shift
            ;;
        --channel)
            need_value "$@"
            [[ "$2" == 2 ]] || fail "CANoe2 paired mode supports --channel 2 only."
            shift 2
            ;;
        --channel=*)
            [[ "${1#*=}" == 2 ]] || fail "CANoe2 paired mode supports --channel 2 only."
            shift
            ;;
        -x|--execute)
            execute=true
            shift
            ;;
        --probe-only)
            probe_only=true
            shift
            ;;
        --capture-reference-baseline)
            # This is the default when no reference log was supplied.
            shift
            ;;
        --exclude-interrupted-case|--accept-operator-door-open)
            fail "Use canoe-paired-wsl.sh directly for incident-specific case exclusion."
            ;;
        *)
            forward+=("$1")
            shift
            ;;
    esac
done

[[ -n "$method" ]] || fail "Choose a mutation method with -m."
[[ "$count" =~ ^[0-9]+$ ]] || fail "Choose a case count from 1 to 20 with -n."
count_number=$((10#$count))
((count_number >= 1 && count_number <= 20)) || fail "Case count must be from 1 to 20."
case "$method" in
    signal_single|signal_combination|state_contradiction|undefined_enum|\
    undefined_bit_single|undefined_bit_multi|defined_undefined_mix|temporal_sequence)
        ;;
    *) fail "Unknown mutation method: $method" ;;
esac
if "$execute" && "$probe_only"; then
    fail "Choose either -x or --probe-only."
fi
[[ -f "$cfg" ]] || fail "CANoe configuration not found: $cfg"
if [[ -z "$campaign" ]]; then
    campaign="$repo_dir/pi_can_lab/logs/canoe_paired/$method"
fi
[[ "$case_start" =~ ^[0-9]+$ ]] || fail "Case start must be a positive number."
case_start_number=$((10#$case_start))
((case_start_number >= 1)) || fail "Case start must be a positive number."
if ((case_start_number > 1)) && [[ ! -f "$campaign/campaign.json" ]]; then
    fail "No existing campaign for case $case_start_number: $campaign. Start at case 1."
fi

python_exe="$repo_dir/.venv-win/Scripts/python.exe"
if [[ ! -f "$python_exe" ]]; then
    echo "[SETUP] Windows Python environment is missing. Run once in WSL:" >&2
    echo "  py.exe -3 -m venv \"$(wslpath -w "$repo_dir/.venv-win")\"" >&2
    echo "  \"$python_exe\" -m pip install -r \"$(wslpath -w "$repo_dir/pi_can_lab/requirements-canoe.txt")\"" >&2
    exit 2
fi

mkdir -p "$repo_dir/pi_can_lab/canoe/logs/capture"

arguments=(--channel 2 --canoe-config "$cfg" --campaign-dir "$campaign" -m "$method")
arguments+=(-n "$count_number")
arguments+=("${forward[@]}")
if [[ -n "$reference" ]]; then
    arguments+=(--reference-baseline-log "$reference")
    echo "[REFERENCE] Existing BLF: $reference"
else
    arguments+=(--capture-reference-baseline)
    echo "[REFERENCE] 30-second receive-only baseline on a new campaign; saved pair reused on resume"
fi
echo "[CONFIG] $cfg"
if [[ "$cfg" != "$repo_dir/pi_can_lab/canoe/config/ThreeChannelCAN.cfg" ]]; then
    echo "[CONFIG] External CFG selected; ensure its BLF/ASC logging directory exists"
fi
echo "[CAMPAIGN] $campaign"

runner="$script_dir/canoe-paired-wsl.sh"
if "$execute"; then
    echo "[PREFLIGHT] Capturing reference if needed, then 6-second passive probe; no Tx yet"
    bash "$runner" "${arguments[@]}" --probe-only
    echo "[EXECUTE] Passive probe validated; starting paired mutation/no-op captures"
    bash "$runner" "${arguments[@]}" -x
elif "$probe_only"; then
    bash "$runner" "${arguments[@]}" --probe-only
else
    bash "$runner" "${arguments[@]}"
fi
