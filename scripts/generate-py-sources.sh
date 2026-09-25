#!/usr/bin/env bash
set -euo pipefail
runtime=$(sed -n "s/^runtime-version: '\(.*\)'$/\1/p" net.ankiweb.Anki.yaml)
uv pip compile ./requirements.in --universal --python-version 3.13 --generate-hashes -o ./python-requirements.txt
uv run ./flatpak-builder-tools/pip/flatpak-pip-generator.py --runtime="org.kde.Sdk//$runtime" --requirements-file=./python-requirements.txt --checker-data -o python3-modules.json --prefer-wheels=rpds-py,orjson,markupsafe
