# Map-Reduce for Code Review: LiteLLM Implementation

## The Problem
Pull Requests can exceed 100k+ lines, surpassing the context window of most models or causing massive costs. Naive chunking creates hallucinations because the model lacks context of the global diff.

## Our Solution: LiteLLM Managed Pipeline
We use **LiteLLM** to decouple our algorithm from the AI provider, allowing us to route different phases to the most efficient models.

### 1. The Map Phase
## Optimized Gemini Pipeline
| Phase | Model | Key Advantage |
|---|---|---|
| **Map** | `gemini-2.0-flash` | High concurrency, zero-cost pattern matching. |
| **Reduce** | `gemini-1.5-pro` | Massive context window for whole-PR reasoning. |

### 2. The Reduce Phase
- **Process:** We collect all summaries from the Map phase. We do NOT send the raw code again. Instead, we send the *summaries* to a high-reasoning model.
- **Constraint:** We use a "Synthesis Prompt" that explicitly forbids the model from inferring missing variables or dependencies between chunks.
- **Output:** A global summary and risk level that represents the entire PR.

### 3. Unified Tool Calling
LiteLLM allows us to use a single tool-definition format.
- We enforce `tool_choice="required"`.
- This ensures that whether we use Gemini, OpenAI, or Anthropic, the response always fits our Pydantic `ReviewResult` model, preventing parsing errors.

## Cost Efficiency Example
- **Traditional Method:** Sending a 200k token diff to Claude 3.5 Sonnet once -> **~$3.00**.
- **Revix Map-Reduce:**
    - Map (20 chunks x Gemini Flash) -> **~$0.02**.
    - Reduce (Summaries x Claude Sonnet) -> **~$0.15**.
    - **Total: ~$0.17 (94% Savings)**.
