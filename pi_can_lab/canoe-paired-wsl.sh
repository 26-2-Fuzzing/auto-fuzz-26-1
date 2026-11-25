#!/usr/bin/env bash
# Launch the paired CANoe capture using the Windows Python environment.
set -euo pipefail

if ! command -v wslpath >/dev/null 2>&1; then
    echo "This launcher must run inside WSL." >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$script_dir/.." && pwd)"
python_exe="$repo_dir/.venv-win/Scripts/python.exe"
if [[ ! -f "$python_exe" ]]; then
    echo "Windows Python environment not found: $python_exe" >&2
    exit 2
fi

arguments=()
while (($#)); do
    case "$1" in
        --config|-c|--canoe-config|--reference-baseline-log|--campaign-dir)
            option="$1"
            shift
            if (($# == 0)); then
                echo "Missing path after $option" >&2
                exit 2
            fi
            arguments+=("$option" "$(wslpath -w "$1")")
            ;;
        --config=*|--canoe-config=*|--reference-baseline-log=*|--campaign-dir=*)
            option="${1%%=*}"
            value="${1#*=}"
            arguments+=("$option=$(wslpath -w "$value")")
            ;;
        *)
            arguments+=("$1")
            ;;
    esac
    shift
done

windows_script="$(wslpath -w "$script_dir/canoe_paired_cycle.py")"
exec "$python_exe" -B "$windows_script" "${arguments[@]}"
