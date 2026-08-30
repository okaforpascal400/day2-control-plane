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
#   stale-base   sbom-rescan / CVE response agent      a shipped, fixable HIGH
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

  stale-base   app/web/Dockerfile             -> drop the CVE-2026-14456 bridge
               expected gate: image (web) / Scan image (trivy), AND a finding
               for the CVE response agent to answer. This is the Phase 5
               scenario and it is different in kind from the four above: those
               break CI, this one restores a state this repository genuinely
               shipped from — the runtime image before commit 4799195 patched
               libssl3/libcrypto3. ci goes red at the trivy gate, but ci.yml
               uploads the SBOM *before* that gate, so the artifact exists on
               the red run and sbom-rescan.yml can be pointed at it with
               `-f source_run=<that run id>`.

  restore      revert every file any scenario touched
  list         this message

Typical loop:
  git switch -c phase4/scenario-bad-dep
  scripts/break.sh bad-dep
  git commit -am 'test(triage): seed a bad-dep failure' && git push -u origin HEAD
  gh pr create --fill   # ci.yml runs on pull_request; a push alone triggers nothing
  # ...CI fails, triage-agent.yml fires, a PR appears...
  scripts/break.sh restore && git commit -am 'test(triage): restore' && git push

The Phase 5 loop is the same up to the red run, then points the rescan at it:
  git switch -c phase5/scenario-stale-base
  scripts/break.sh stale-base
  git commit -am 'test(cve): seed a stale-base finding' && git push -u origin HEAD
  gh pr create --fill                     # ci goes red at image (web)
  gh workflow run sbom-rescan.yml \
     -f scenario_ref=phase5/scenario-stale-base -f source_run=<red ci run id>
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
    stale-base)
        echo "Seeding stale-base — expect: image (web) / Scan image (trivy),"
        echo "  and a fixable HIGH in the web SBOM for the CVE response agent."
        # The whole block, comment included. Leaving the comment behind would
        # hand the agent its own answer — it names the CVE, the package and the
        # fixed version — and a demo that tells the model what to conclude has
        # verified nothing.
        replace_in "app/web/Dockerfile" \
            "# Base-image lag, not app code: nginx-unprivileged:1.31-alpine still ships
# libssl3/libcrypto3 3.5.7-r0, carrying a fixable HIGH (CVE-2026-14456, OpenSSL
# QUIC DoS) that the trivy gate rejects. No rebuilt nginx-unprivileged alpine tag
# carries the fix yet, so pull the patched 3.5.8-r0 straight from Alpine's 3.24
# repo. This is a bridge: once upstream republishes with >=3.5.8-r0 it becomes a
# no-op and should be replaced by a plain base-digest bump.
USER root
RUN apk add --no-cache --upgrade libssl3 libcrypto3

" ""
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
