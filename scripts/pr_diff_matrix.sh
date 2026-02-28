#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <pr_number> <pr_number> [more_prs...]" >&2
  echo "Example: $0 101 111 114 115 122" >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not inside a git repository" >&2
  exit 1
fi

REMOTE_NAME="${PR_REMOTE:-origin}"
BASE_BRANCH="${PR_BASE:-main}"
BASE_REF="${REMOTE_NAME}/${BASE_BRANCH}"

if ! git remote get-url "${REMOTE_NAME}" >/dev/null 2>&1; then
  echo "ERROR: remote '${REMOTE_NAME}' not found." >&2
  echo "Please run the following commands first:" >&2
  echo "  git remote -v" >&2
  echo "  git remote add ${REMOTE_NAME} <repo_url>" >&2
  echo "  git fetch ${REMOTE_NAME} --prune" >&2
  exit 2
fi

git fetch "${REMOTE_NAME}" --prune >/dev/null

if ! git rev-parse --verify --quiet "${BASE_REF}" >/dev/null; then
  echo "ERROR: base ref '${BASE_REF}' not found." >&2
  echo "Try: PR_BASE=master bash $0 $*" >&2
  exit 3
fi

for pr in "$@"; do
  local_ref="pr-${pr}"
  if git show-ref --verify --quiet "refs/heads/${local_ref}"; then
    git branch -D "${local_ref}" >/dev/null
  fi
  git fetch "${REMOTE_NAME}" "pull/${pr}/head:${local_ref}" >/dev/null
  echo "Fetched PR #${pr} -> ${local_ref}"
done

declare -A FILE_LIST

for pr in "$@"; do
  ref="pr-${pr}"
  file_out="/tmp/pr_${pr}_files.txt"
  git diff --name-only "${BASE_REF}...${ref}" | sort -u > "${file_out}"
  FILE_LIST["$pr"]="${file_out}"
  count=$(wc -l < "${file_out}" | tr -d ' ')
  echo
  echo "PR #${pr} changed files: ${count}"
  sed 's/^/  - /' "${file_out}"
done

echo
echo "Overlap matrix (shared file count):"
printf "%-8s" "PR"
for pr in "$@"; do printf "%-8s" "#${pr}"; done
echo

for pr_a in "$@"; do
  printf "%-8s" "#${pr_a}"
  for pr_b in "$@"; do
    if [[ "${pr_a}" == "${pr_b}" ]]; then
      printf "%-8s" "-"
      continue
    fi
    overlap=$(comm -12 "${FILE_LIST[$pr_a]}" "${FILE_LIST[$pr_b]}" | wc -l | tr -d ' ')
    printf "%-8s" "${overlap}"
  done
  echo
done

echo
echo "Tip: overlap > 0 means file-level conflict risk."
echo "Tip: if remote uses 'master', run with: PR_BASE=master bash $0 $*"
