#!/usr/bin/env bash
# linuxdeploy recipe — bundling driver invoked by build-appimage.sh.
#
# P.1a: placeholder. The real implementation lands in P.1b (Python +
# pure-Python deps) and P.2a (GTK4 + libadwaita via
# linuxdeploy-plugin-gtk). See docs/plans/desktop-appimage-packaging-plan.md.
#
# Expected env when called by build-appimage.sh:
#   APPDIR        — staging dir to populate (already created)
#   SOURCE_DIR    — desktop-connector checkout root
#   TOOLS_DIR     — vendored linuxdeploy AppImages
#   OUTPUT_DIR    — where the final .AppImage lands
#   APP_VERSION   — string from version.json (desktop field)
set -euo pipefail

echo "linuxdeploy.recipe.sh: P.1a stub — no bundling performed yet."
echo "  APPDIR=${APPDIR:-<unset>}"
echo "  SOURCE_DIR=${SOURCE_DIR:-<unset>}"
echo "  TOOLS_DIR=${TOOLS_DIR:-<unset>}"
echo "  OUTPUT_DIR=${OUTPUT_DIR:-<unset>}"
echo "  APP_VERSION=${APP_VERSION:-<unset>}"
exit 0
