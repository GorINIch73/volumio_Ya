#!/bin/sh
set -eu
# Volumio calls onStop and removes the plugin files/configuration itself.
# No system service, sudo rule or files outside the plugin are installed.
echo "YaM: plugin removal is managed by Volumio."
echo "pluginuninstallend"
