#!/bin/sh
set -e

cd "$(dirname "$0")/.."

echo "building murmur-bridge..."
go build -o bin/murmur-bridge.exe ./cmd/murmur-bridge

echo "built: bin/murmur-bridge.exe"