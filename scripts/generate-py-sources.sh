#!/usr/bin/env bash
set -euo pipefail
runtime=$(sed -n "s/^runtime-version: '\(.*\)'$/\1/p" net.ankiweb.Anki.yaml)
uv export --directory anki --quiet --frozen --package aqt --package anki --no-dev \
  --no-emit-workspace \
  --no-emit-package pyqt6 --no-emit-package pyqt6-qt6 --no-emit-package pyqt6-sip \
  --no-emit-package pyqt6-webengine --no-emit-package pyqt6-webengine-qt6 \
  --output-file "$PWD/python-requirements.txt"
# flatpak-pip-generator only evaluates platform markers like sys_platform, so a
# python/arch-specific dependency would be packaged wrongly. Stop and handle it by hand.
if grep -nE ';.*(python_(full_)?version|platform_machine)' python-requirements.txt >&2; then
  echo "error: python-requirements.txt has Python version or arch markers that" \
    "flatpak-pip-generator cannot evaluate; see updating-anki.md, step 4" >&2
  exit 1
fi
uv run ./flatpak-builder-tools/pip/flatpak-pip-generator.py --runtime="org.kde.Sdk//$runtime" --requirements-file=./python-requirements.txt --checker-data -o python3-modules.json --prefer-wheels=rpds-py,orjson,markupsafe --ignore-installed=markupsafe
