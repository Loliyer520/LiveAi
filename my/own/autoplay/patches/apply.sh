#!/bin/bash
# Apply 26.2 protocol patches to minecraft-data in node_modules
# Usage: bash patches/apply.sh

set -e

NODE_MODULES_PROTO="$(dirname "$0")/../node_modules/minecraft-data/minecraft-data/data/pc/26.2/protocol.json"
PATCHED_PROTO="$(dirname "$0")/protocol_26.2_patched.json"

if [ ! -f "$PATCHED_PROTO" ]; then
    echo "ERROR: Patched protocol.json not found at $PATCHED_PROTO"
    exit 1
fi

# Backup original if not already backed up
if [ ! -f "${NODE_MODULES_PROTO}.orig" ]; then
    cp "$NODE_MODULES_PROTO" "${NODE_MODULES_PROTO}.orig"
    echo "Original protocol.json backed up to ${NODE_MODULES_PROTO}.orig"
fi

cp "$PATCHED_PROTO" "$NODE_MODULES_PROTO"
echo "Patched protocol.json applied to $NODE_MODULES_PROTO"
echo "Done. Restart the bot to test."
