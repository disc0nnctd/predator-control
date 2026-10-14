#!/bin/sh
# Refresh the bundle's copies of the app files (tests/check.py fails if they drift).
cd "$(dirname "$0")/.." && cp server.py index.html deploy/predator/
