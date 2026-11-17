#!/bin/bash
set -euo pipefail

: "${EESSI_SUBMIT_PARTITION:?set the intended partition}"
check=(--check-submit --partition "$EESSI_SUBMIT_PARTITION")
submit=(--partition "$EESSI_SUBMIT_PARTITION")
if [[ -n "${EESSI_SUBMIT_CONSTRAINT:-}" ]]; then
    check+=(--constraint "$EESSI_SUBMIT_CONSTRAINT")
    submit+=(--constraint "$EESSI_SUBMIT_CONSTRAINT")
fi

eessi-arch-guard "${check[@]}"
exec sbatch "${submit[@]}" "$@"
