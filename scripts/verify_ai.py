import asyncio
import logging
import sys

from litellm import acompletion

from app.config import settings
from app.services.ai import AIService

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def test_ai_connectivity() -> None:
    print("\n🔍 Testing AI API Connectivity (Palantir Standard)...")
    print(f"📦 Map Model:    {settings.AI_MODEL_MAP}")
    print(f"📦 Reduce Model: {settings.AI_MODEL_REDUCE}")

    if not settings.AI_API_KEY:
        print("❌ Error: No API key found. Please set AI_API_KEY in your .env file.")
        sys.exit(1)

    masked_key = f"{settings.AI_API_KEY[:6]}...{settings.AI_API_KEY[-4:]}"
    print(f"🔑 API Key:      {masked_key}")

    ai_service = AIService()
    kwargs = ai_service._get_completion_kwargs(settings.AI_MODEL_MAP)
    kwargs["messages"] = [
        {
            "role": "user",
            "content": "Ping from Revix engine. Reply with 'PONG' and active status.",
        }
    ]
    kwargs["max_tokens"] = 500

    print("\n⏳ Dispatching test request...")
    try:
        response = await acompletion(**kwargs)
        msg = response.choices[0].message
        content = (msg.content or msg.reasoning_content or "").strip()
        print(f'✅ Response: "{content}"')
        print("✨ AI provider connectivity verified successfully.")
    except Exception as e:
        print("\n❌ API Request Failed!")
        print(f"Error details: {e}")
        print("\n💡 Troubleshooting:")
        print("1. Verify your AI_API_KEY in .env.")
        print(f"2. Confirm the model '{settings.AI_MODEL_MAP}' is accessible with your key.")
        print("3. Check proxy / network connectivity.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(test_ai_connectivity())
    except KeyboardInterrupt:
        print("\nTerminated by user.")
        sys.exit(0)
