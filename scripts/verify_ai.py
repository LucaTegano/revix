import asyncio
import logging
import sys

from litellm import acompletion

from app.config import settings

# Setup basic logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def test_ai_connectivity():
    print("\n🔍 Testing AI API Connectivity...")
    print(f"📦 Model:    {settings.AI_MODEL_MAP}")

    # Check for API Key
    api_key = settings.active_api_key
    if not api_key:
        print("❌ Error: No API key found. Please set AI_API_KEY in your .env file.")
        sys.exit(1)

    print(f"🔑 API Key:  {api_key[:5]}...{api_key[-4:] if len(api_key) > 8 else ''}")

    # Prepare completion arguments
    kwargs = {
        "model": settings.AI_MODEL_MAP,
        "messages": [
            {
                "role": "user",
                "content": "Hello, this is a connectivity test. Reply with 'OK' if you can hear me.",
            }
        ],
        "api_key": api_key,
        "max_tokens": 10,
    }

    # Handle OpenRouter specific base
    if settings.AI_MODEL_MAP.startswith("openrouter/"):
        kwargs["api_base"] = "https://openrouter.ai/api/v1"
        kwargs["extra_headers"] = {
            "HTTP-Referer": "https://github.com/LucaTegano/revix",
            "X-Title": "Revix Connectivity Test",
        }

    print("\n⏳ Sending test request...")
    try:
        response = await acompletion(**kwargs)
        content = response.choices[0].message.content.strip()
        print(f'✅ Success! Response: "{content}"')
        print("\n✨ Your AI configuration is correct.")
    except Exception as e:
        print("\n❌ API Request Failed!")
        print(f"Error details: {str(e)}")
        print("\n💡 Troubleshooting tips:")
        print("1. Check if your AI_API_KEY is correct.")
        print(f"2. Ensure the model name '{settings.AI_MODEL_MAP}' is supported by your provider.")
        print("3. Check your internet connection or proxy settings.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(test_ai_connectivity())
    except KeyboardInterrupt:
        print("\nTerminated by user.")
        sys.exit(0)
