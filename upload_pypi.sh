#!/bin/bash
set -e

# ConfigBack - PyPI Upload Script (Linux / macOS)
# Usage:
#   ./upload_pypi.sh          Upload to PyPI
#   ./upload_pypi.sh --test   Upload to TestPyPI

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== ConfigBack PyPI Upload ==="

# Parse arguments
REPO_FLAG=""
if [ "$1" = "--test" ]; then
    REPO_FLAG="--repository testpypi"
    echo "Target: TestPyPI"
else
    echo "Target: PyPI"
fi

# Clean old builds
echo "Cleaning old builds..."
rm -rf dist/ build/ *.egg-info

# Install build tools
echo "Installing build tools..."
pip install --upgrade build twine

# Build
echo "Building package..."
python -m build

# Check
echo "Checking package..."
twine check dist/*

# Upload
echo "Uploading..."
twine upload $REPO_FLAG dist/*

echo ""
echo "=== Upload complete! ==="
if [ "$1" = "--test" ]; then
    echo "View at: https://test.pypi.org/project/configback/"
else
    echo "View at: https://pypi.org/project/configback/"
fi
