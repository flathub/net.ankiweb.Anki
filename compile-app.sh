#!/usr/bin/env bash
flatpak-builder --repo=./repo build-dir ./net.ankiweb.Anki.yaml --force-clean --ccache $@
