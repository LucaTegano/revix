#!/bin/bash
set -e

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Working directory to project root
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

echo -e "${GREEN}🚀 Revix Setup (Palantir-Grade Standard)${NC}"
echo -e "----------------------------------------"

# 1. Dependency Checks
echo -e "\n🔍 Checking system dependencies..."
if ! command -v uv &> /dev/null; then
    echo -e "${RED}❌ 'uv' is required for fast, deterministic builds.${NC}"
    echo -e "👉 Install it via: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}⚠️  Docker not detected. Required for 'make db' and local sandboxing.${NC}"
fi

# 2. Environment Initialization
if [ ! -f .env ]; then
    echo -e "\n📄 Creating .env from .env.example..."
    cp .env.example .env
    echo -e "${GREEN}✅ .env file created.${NC}"
else
    echo -e "ℹ️  .env file already exists."
fi

# 3. GitHub Private Key Auto-Detection
if [ -f "github_private_key.pem" ]; then
    echo -e "${BLUE}🔑 Detected 'github_private_key.pem' in project root.${NC}"
    if ! grep -q "^GITHUB_APP_PRIVATE_KEY_PATH=" .env; then
        echo "GITHUB_APP_PRIVATE_KEY_PATH=./github_private_key.pem" >> .env
        echo -e "${GREEN}✅ Added GITHUB_APP_PRIVATE_KEY_PATH=./github_private_key.pem to .env${NC}"
    fi
fi

# 4. Sync Dependencies
echo -e "\n📦 Syncing dependencies with uv..."
uv sync

# 5. Validation
echo -e "\n🧪 Validating configuration..."
if uv run python3 -m app.config; then
    echo -e "\n${GREEN}✨ Setup complete! Environment is ready.${NC}"
    echo -e "👉 Next steps:"
    echo -e "   1. 'make db'          - Start PostgreSQL container"
    echo -e "   2. 'make migrations'  - Apply database schema"
    echo -e "   3. 'make test-ai'     - Verify your AI model connectivity"
    echo -e "   4. 'make web'         - Launch API server"
else
    echo -e "\n${YELLOW}ℹ️  Please fill in your API and GitHub keys in .env${NC}"
    echo -e "👉 Then run: uv run python3 -m app.config"
fi
