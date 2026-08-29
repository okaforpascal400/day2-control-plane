#!/usr/bin/env bash
#
# Seed a reproducible CI failure, so the triage agent has something real to
# diagnose. Four scenarios, each chosen to fail at a *different* gate — a
# triage agent that only ever sees one kind of failure has not been tested.
#
#   bad-dep      pytest (api) / Install dependencies   dependency resolution
#   fail-test    pytest (api) / Run tests              a wrong assertion
#   bad-env      helm lint / chart env contract        chart-vs-code config drift
#   vuln-image   image (api) / Scan image (trivy)      a base image with HIGH CVEs
#
# Usage:
#   scripts/break.sh <scenario>   apply the break (working tree only)
#   scripts/break.sh restore      put every touched file back
#   scripts/break.sh list         show the scenarios and their expected gate
#
# The originals are copied to .break-backup/ (gitignored) before anything is
# edited, so `restore` works whether or not the break has been committed and
# does not depend on git state. Commit and push the break yourself — this
# script never touches git, by design: a script that both breaks the build and
# pushes is one typo away from doing it on the wrong branch.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${REPO_ROOT}/.break-backup"
MANIFEST="${BACKUP_DIR}/MANIFEST"

# A base image with known, *fixed* HIGH CVEs — trivy runs with --ignore-unfixed,
# so an unfixable finding would not fail the gate. Pinned by digest like every
# other image here; this one is deliberately old, not deliberately unpinned.
VULN_IMAGE="python:3.11-slim-bullseye@sha256:9e25f400253a5fa3191813d6a67eb801ca1e6f012b3bd2588fa6920b59e3eba6"
GOOD_IMAGE="python:3.12-slim@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Seeded CI failure scenarios:

  bad-dep      app/api/requirements-dev.txt   -> a version that does not exist on PyPI
               expected gate: pytest (api) / Install dependencies

  fail-test    app/api/tests/test_health.py   -> an assertion the code never promised
               expected gate: pytest (api) / Run tests

  bad-env      deploy/helm/templates/worker.yaml -> DAY2_BATCH_SIZE misspelled
               expected gate: helm lint / chart env contract

  vuln-image   app/api/Dockerfile             -> an old base image with HIGH CVEs
               expected gate: image (api) / Scan image (trivy)

  restore      revert every file any scenario touched
  list         this message

Typical loop:
  git switch -c phase4/scenario-bad-dep
  scripts/break.sh bad-dep
  git commit -am 'test(triage): seed a bad-dep failure' && git push -u origin HEAD
  # ...CI fails, triage-agent.yml fires, a PR appears...
  scripts/break.sh restore && git commit -am 'test(triage): restore' && git push
EOF
}

# Copy a file aside before it is edited, once, and note it in the manifest.
back_up() {
    local rel="$1"
    local src="${REPO_ROOT}/${rel}"
    [[ -f "$src" ]] || die "no such file: ${rel}"
    local dest="${BACKUP_DIR}/${rel}"
    if [[ ! -f "$dest" ]]; then
        mkdir -p "$(dirname "$dest")"
        cp "$src" "$dest"
        printf '%s\n' "$rel" >>"$MANIFEST"
    fi
}

# sed -i, but it fails loudly when the pattern was not there. A break that
# silently no-ops produces a green CI run and a very confusing afternoon.
replace_in() {
    local rel="$1" from="$2" to="$3"
    local path="${REPO_ROOT}/${rel}"
    grep -qF -- "$from" "$path" \
        || die "pattern not found in ${rel}: ${from}
The file has changed since this scenario was written; update scripts/break.sh."
    back_up "$rel"
    python3 - "$path" "$from" "$to" <<'PY'
import pathlib, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
p.write_text(p.read_text().replace(old, new, 1))
PY
    printf '  %s\n' "${rel}"
}

restore() {
    [[ -f "$MANIFEST" ]] || { echo "Nothing to restore."; return 0; }
    while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        cp "${BACKUP_DIR}/${rel}" "${REPO_ROOT}/${rel}"
        printf '  restored %s\n' "$rel"
    done <"$MANIFEST"
    rm -rf "$BACKUP_DIR"
    echo "Working tree is back to its pre-break state."
}

mkdir -p "$BACKUP_DIR"
touch "$MANIFEST"

case "${1:-}" in
    bad-dep)
        echo "Seeding bad-dep — expect: pytest (api) / Install dependencies"
        replace_in "app/api/requirements-dev.txt" \
            "httpx==0.28.1" "httpx==0.28.9999"
        ;;
    fail-test)
        echo "Seeding fail-test — expect: pytest (api) / Run tests"
        replace_in "app/api/tests/test_health.py" \
            'assert response.json() == {"status": "ok", "service": "api-test"}' \
            'assert response.json() == {"status": "healthy", "service": "api-test"}'
        ;;
    bad-env)
        echo "Seeding bad-env — expect: helm lint / chart env contract"
        replace_in "deploy/helm/templates/worker.yaml" \
            "- name: DAY2_BATCH_SIZE" "- name: DAY2_BATCH_SIZ"
        ;;
    vuln-image)
        echo "Seeding vuln-image — expect: image (api) / Scan image (trivy)"
        replace_in "app/api/Dockerfile" \
            "ARG PYTHON_IMAGE=${GOOD_IMAGE}" "ARG PYTHON_IMAGE=${VULN_IMAGE}"
        ;;
    restore)
        restore
        ;;
    list|--help|-h|"")
        usage
        ;;
    *)
        die "unknown scenario '${1}'. Run 'scripts/break.sh list'."
        ;;
esac
