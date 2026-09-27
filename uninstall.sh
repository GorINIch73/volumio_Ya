#!/bin/bash
set -euo pipefail
# Volumio calls onStop and removes the plugin files/configuration itself.
# No system service, sudo rule or files outside the plugin are installed.
echo "Media Str: plugin removal is managed by Volumio."
echo "pluginuninstallend"
