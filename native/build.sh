#!/bin/bash
# build.sh - Build the pw_bridge C wrapper.

set -e
cd "$(dirname "$0")"

if ! pkg-config --exists libpipewire-0.3; then
    echo "Error: libpipewire-0.3 not found."
    echo "Install libpipewire-0.3-dev (Debian/Ubuntu) or the equivalent."
    exit 1
fi

make clean
make

echo "Wrapper built: $(pwd)/libpw_bridge.so"