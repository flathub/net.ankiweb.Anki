uv pip compile ./requirements.in --universal --python-version 3.13 --generate-hashes -o ./python-requirements.txt
uv run ./flatpak-builder-tools/pip/flatpak-pip-generator.py --runtime=org.kde.Sdk//6.10 --requirements-file=./python-requirements.txt --checker-data -o python3-modules.json --prefer-wheels=rpds-py,orjson,markupsafe

