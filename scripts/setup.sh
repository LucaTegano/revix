#!/bin/bash
set -e

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Fix working directory to project root
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

echo -e "${GREEN}🚀 Revix Setup Wizard${NC}"
echo -e "---------------------"

# 1. Dependency Checks
echo -e "\n🔍 Checking dependencies..."

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ python3 is not installed.${NC}"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}⚠️  Docker is not installed. You won't be able to run 'make db'.${NC}"
fi

# 2. Environment File Creation
if [ ! -f .env ]; then
    echo -e "\n📄 Creating .env from .env.example..."
    cp .env.example .env
    echo -e "${GREEN}✅ .env file created.${NC}"
else
    echo -e "\nℹ️  .env file already exists. Merging missing variables..."
    grep -v '^#' .env.example | grep -v '^$' | while IFS='=' read -r key value; do
        if ! grep -q "^${key}=" .env; then
            echo "${key}=${value}" >> .env
            echo "  + Added missing key: ${key}"
        fi
    done
    echo -e "${GREEN}✅ .env file updated.${NC}"
fi

# 3. Interactive Prompts (Optional but helpful)
read -p "❓ Do you want to configure required keys now? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Helper to update .env
    update_env() {
        local key=$1
        local value=$2
        # Use sed to update the value. Handles different sed versions on Mac/Linux
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|^$key=.*|$key=$value|" .env
        else
            sed -i "s|^$key=.*|$key=$value|" .env
        fi
    }

    echo -e "\n--- API Key ---"
    read -p "Enter your API Key (AI_API_KEY): " key
    update_env "AI_API_KEY" "$key"

    echo -e "\n--- GitHub App ---"
    read -p "GitHub App ID: " app_id
    update_env "GITHUB_APP_ID" "$app_id"
    
    read -p "GitHub Webhook Secret: " webhook_secret
    update_env "GITHUB_WEBHOOK_SECRET" "$webhook_secret"
    
    read -p "GitHub App Private Key (Base64): " b64_key
    update_env "GITHUB_APP_PRIVATE_KEY_B64" "$b64_key"
fi

# 4. Final Validation
echo -e "\n🧪 Validating configuration..."

# Prefer 'uv run' if uv is installed
if command -v uv &> /dev/null; then
    echo -e "📦 Using 'uv run' for validation..."
    if uv run python3 -m app.config; then
        echo -e "\n${GREEN}✨ Setup complete! You are ready to go.${NC}"
        echo -e "👉 Next steps: 'make db' then 'make migrations' then 'make web'"
    else
        echo -e "\n${RED}❌ Configuration is invalid. Please check your .env file.${NC}"
        exit 1
    fi
else
    # Fallback to system python but warn the user
    echo -e "${YELLOW}⚠️  'uv' not found. Falling back to system python (may fail if dependencies are missing).${NC}"
    if python3 -m app.config; then
        echo -e "\n${GREEN}✨ Setup complete! You are ready to go.${NC}"
        echo -e "👉 Next steps: 'make db' then 'make migrations' then 'make web'"
    else
        echo -e "\n${RED}❌ Configuration is invalid. Please check your .env file.${NC}"
        echo -e "${YELLOW}💡 Tip: Try running 'make install' first to install dependencies.${NC}"
        exit 1
    fi
fi
