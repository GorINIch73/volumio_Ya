#!/bin/sh
set -eu
# Volumio calls onStop and removes the plugin files/configuration itself.
cd -- "$(dirname -- "$0")"
sudo python3 python/ui_extension.py remove
# Reload open browsers after removal to unload the UI hook.
echo "YaM: plugin removal is managed by Volumio."
echo "pluginuninstallend"
