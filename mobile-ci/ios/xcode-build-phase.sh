#!/usr/bin/env bash
# Xcode "Run Script" build phase: upload dSYMs to self-hosted Sentry after every
# Release build. Add it as the LAST phase of the app target and set
# "Based on dependency analysis" off. Provide SENTRY_AUTH_TOKEN through a
# non-versioned xcconfig or the CI environment, never in the project file.
#
# Input files (so Xcode knows when to re-run): $(DWARF_DSYM_FOLDER_PATH)/$(DWARF_DSYM_FILE_NAME)
set -euo pipefail

if [[ "${CONFIGURATION:-}" != "Release" ]]; then
  echo "Sentry: skipping dSYM upload for ${CONFIGURATION:-unknown} configuration"
  exit 0
fi

: "${SENTRY_URL:?set SENTRY_URL to your self-hosted Sentry, e.g. https://sentry.example.com/}"
: "${SENTRY_AUTH_TOKEN:?set SENTRY_AUTH_TOKEN}"
: "${SENTRY_ORG:?set SENTRY_ORG}"
: "${SENTRY_PROJECT:?set SENTRY_PROJECT}"

SENTRY_CLI="${SENTRY_CLI:-$(command -v sentry-cli || true)}"
if [[ -z "$SENTRY_CLI" && -x "${PODS_ROOT:-}/Sentry/bin/sentry-cli" ]]; then
  SENTRY_CLI="${PODS_ROOT}/Sentry/bin/sentry-cli"
fi
if [[ -z "$SENTRY_CLI" ]]; then
  echo "error: sentry-cli not found (brew install getsentry/tools/sentry-cli)" >&2
  exit 1
fi

# DWARF_DSYM_FOLDER_PATH holds the dSYM of this target; frameworks embedded in
# the app land next to it, so upload the whole folder.
"$SENTRY_CLI" debug-files upload \
  --include-sources \
  --wait \
  --org "$SENTRY_ORG" --project "$SENTRY_PROJECT" \
  "$DWARF_DSYM_FOLDER_PATH"

# Fail the build if the app's own binary is still unknown to Sentry.
"$SENTRY_CLI" debug-files check \
  --org "$SENTRY_ORG" --project "$SENTRY_PROJECT" \
  "$DWARF_DSYM_FOLDER_PATH/$DWARF_DSYM_FILE_NAME"
