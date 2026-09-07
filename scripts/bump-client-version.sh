#!/usr/bin/env bash
# bump-client-version.sh — auto-increment client-package.json patch version
# Usage: ./scripts/bump-client-version.sh frontend/public/client-package.json
set -euo pipefail

FILE="${1:?Usage: bump-client-version.sh <path-to-client-package.json>}"

if [ ! -f "$FILE" ]; then
  echo "ERROR: $FILE not found" >&2
  exit 1
fi

# Extract current version, increment patch
CURRENT=$(grep -oP '"version"\s*:\s*"\K[^"]+' "$FILE")
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"
PATCH=$((PATCH + 1))
NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"

# Replace in-place
sed -i "s/\"version\": \"${CURRENT}\"/\"version\": \"${NEW_VERSION}\"/" "$FILE"

echo "Bumped client-package.json: ${CURRENT} → ${NEW_VERSION}"
