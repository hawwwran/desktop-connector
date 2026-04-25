#!/usr/bin/env bash
# linuxdeploy recipe — bundling driver for native libs + GTK4.
#
# P.1b: not yet used. The Python bundling path uses niess/python-appimage
# directly (see build-appimage.sh). linuxdeploy earns its keep in P.2a
# when GTK4 + libadwaita + native deps need to be copied out of the host
# system into the AppDir, with RPATH adjustments.
#
# linuxdeploy-plugin-python (the niess companion plugin) is **deprecated**
# in favour of niess/python-appimage; we don't use it.
#
# Expected env when called by build-appimage.sh (P.2a):
#   APPDIR        — staging dir to populate (already created)
#   SOURCE_DIR    — desktop-connector checkout root
#   TOOLS_DIR     — vendored linuxdeploy AppImages
#   APP_VERSION   — string from version.json (desktop field)
set -euo pipefail

echo "linuxdeploy.recipe.sh: P.1b — not yet wired. GTK4 bundling lands in P.2a."
echo "  APPDIR=${APPDIR:-<unset>}"
echo "  SOURCE_DIR=${SOURCE_DIR:-<unset>}"
echo "  TOOLS_DIR=${TOOLS_DIR:-<unset>}"
echo "  APP_VERSION=${APP_VERSION:-<unset>}"
exit 0
