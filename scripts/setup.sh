#!/bin/bash
set -e

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 LucAI Setup Wizard${NC}"
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

    echo -e "\n--- AI Provider ---"
    read -p "AI Provider (gemini/anthropic/openai/openrouter) [openrouter]: " provider
    provider=${provider:-openrouter}
    update_env "AI_PROVIDER" "$provider"

    case $provider in
        openrouter)
            read -p "OpenRouter API Key: " key
            update_env "OPENROUTER_API_KEY" "$key"
            ;;
        gemini)
            read -p "Gemini API Key: " key
            update_env "GEMINI_API_KEY" "$key"
            ;;
        anthropic)
            read -p "Anthropic API Key: " key
            update_env "ANTHROPIC_API_KEY" "$key"
            ;;
        openai)
            read -p "OpenAI API Key: " key
            update_env "OPENAI_API_KEY" "$key"
            ;;
    esac

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
if python3 -m app.config; then
    echo -e "\n${GREEN}✨ Setup complete! You are ready to go.${NC}"
    echo -e "👉 Next steps: 'make db' then 'make migrations' then 'make web'"
else
    echo -e "\n${RED}❌ Configuration is invalid. Please check your .env file.${NC}"
fi
