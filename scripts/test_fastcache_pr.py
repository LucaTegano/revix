"""Live Head-to-Head Benchmark: Solo LLM vs Revix Multi-Agent Swarm on ALTA/FastCache.

Evaluates a real-world high-concurrency C++ Pull Request on FastCache:
PR: "perf: optimize get() concurrency using shared_lock for multiple readers"

Contains 2 critical defects:
1. Data Race / UB: Changed std::unique_lock to std::shared_lock in get(), but
   items_list_.splice() mutates the list pointers! Concurrent get() calls cause
   heap corruption and SIGSEGV.
2. Use-After-Free / Memory Leak: In set() eviction, items_list_.pop_back() is
   called without erasing from items_map_, leaving a dangling ListIterator.

Compares:
- Mode 1: Solo LLM (Generic monolithic prompt, no AST, no specialization)
- Mode 2: Revix Swarm (Tree-sitter AST, Coordinator, Specialized Review + Security Agents, Structured Synthesis)
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from litellm import acompletion

from app.config import settings
from app.services.ai import AIService

PR_TITLE = "perf: optimize get() concurrency using shared_lock for multiple readers"
PR_BODY = """
This PR improves FastCache throughput under read-heavy workloads:
- Replaces std::unique_lock with std::shared_lock in LRUCache::get() to allow concurrent readers.
- Cleans up LRUCache::set() eviction routine to speed up cache writes.
"""

FASTCACHE_DIFF = """
--- a/src/Cache.cpp
+++ b/src/Cache.cpp
@@ -15,7 +15,7 @@ void LRUCache::set(std::string_view key, std::string_view value) {
     if (items_map_.size() >= capacity_) {
       // Cache is full. Remove least recently used (back of the list)
       auto last = items_list_.back();
-      items_map_.erase(last.first);
+      // Direct pop for faster eviction path
       items_list_.pop_back();
     }
   }
@@ -28,7 +28,7 @@ void LRUCache::set(std::string_view key, std::string_view value) {
 
 std::optional<std::string> LRUCache::get(std::string_view key) {
-  std::unique_lock<std::shared_mutex> lock(mutex_);
+  std::shared_lock<std::shared_mutex> lock(mutex_);
 
   auto it = items_map_.find(std::string(key));
 
@@ -36,7 +36,7 @@ std::optional<std::string> LRUCache::get(std::string_view key) {
     return std::nullopt;
   }
 
-  // Move the accessed item to the front of the list
+  // Move the accessed item to the front of the list
   items_list_.splice(items_list_.begin(), items_list_, it->second);
 
   return it->second->second;
"""

FASTCACHE_FILE_CONTENT = """#include "Cache.h"

LRUCache::LRUCache(size_t capacity) : capacity_(capacity) {}

void LRUCache::set(std::string_view key, std::string_view value) {
  std::unique_lock<std::shared_mutex> lock(mutex_);

  auto it = items_map_.find(std::string(key));

  if (it != items_map_.end()) {
    items_list_.erase(it->second);
  } else {
    if (items_map_.size() >= capacity_) {
      auto last = items_list_.back();
      items_list_.pop_back();
    }
  }

  items_list_.push_front({std::string(key), std::string(value)});
  items_map_[std::string(key)] = items_list_.begin();
}

std::optional<std::string> LRUCache::get(std::string_view key) {
  std::shared_lock<std::shared_mutex> lock(mutex_);

  auto it = items_map_.find(std::string(key));

  if (it == items_map_.end()) {
    return std::nullopt;
  }

  items_list_.splice(items_list_.begin(), items_list_, it->second);

  return it->second->second;
}
"""


async def run_solo_llm(ai_service: AIService) -> tuple[str, float]:
    """Runs a standard, naive single-agent review prompt."""
    prompt = f"""
You are an expert code reviewer. Review the following Pull Request for ALTA/FastCache.
PR TITLE: {PR_TITLE}
PR DESCRIPTION: {PR_BODY}

DIFF:
{FASTCACHE_DIFF}

Provide a code review summary, score from 0-100, and list any issues or suggestions.
"""
    kwargs = ai_service._get_completion_kwargs(settings.AI_MODEL_MAP)
    kwargs["messages"] = [
        {"role": "system", "content": "You are a code review assistant."},
        {"role": "user", "content": prompt},
    ]

    start = time.perf_counter()
    response = await acompletion(**kwargs)
    elapsed = time.perf_counter() - start

    content = response.choices[0].message.content or ""
    return content, elapsed


async def run_revix_swarm(ai_service: AIService) -> tuple[Any, float]:
    """Runs the full Revix Swarm cognitive pipeline."""
    pr_files = [
        {
            "filename": "src/Cache.cpp",
            "patch": FASTCACHE_DIFF,
            "content": FASTCACHE_FILE_CONTENT,
        }
    ]
    pr_details = {
        "title": PR_TITLE,
        "body": PR_BODY,
    }

    start = time.perf_counter()
    result = await ai_service.analyze_diff(
        diff=FASTCACHE_DIFF,
        repo_full_name="ALTA/FastCache",
        pr_files=pr_files,
        pr_details=pr_details,
    )
    elapsed = time.perf_counter() - start
    return result, elapsed


def check_detected_bugs(review_text: str) -> dict[str, bool]:
    text_lower = review_text.lower()

    # Bug 1: Concurrency race with splice & shared_lock
    # Must flag that mutating the list/splice under shared_lock is unsafe, a data race, or violates thread safety
    has_splice_shared = ("splice" in text_lower or "shared_lock" in text_lower)
    flags_as_unsafe = any(term in text_lower for term in [
        "data race", "data-race", "race condition", "violates", "cannot truly run in parallel",
        "heap corruption", "not thread-safe", "unsafe", "dereference a dangling"
    ]) and not ("is safe under shared_lock" in text_lower or "conceptually sound" in text_lower)
    # Check if specific critical comment exists or explicit race identified
    bug1_detected = has_splice_shared and (flags_as_unsafe or "violates the mutex" in text_lower or "data race" in text_lower)

    # Bug 2: Missing erase from items_map_
    bug2_detected = any(term in text_lower for term in ["items_map", "erase", "dangling", "stale", "iterator"]) and any(term in text_lower for term in ["leak", "missing", "pop_back", "memory", "uaf", "use-after-free"])

    return {
        "Bug 1 (Data Race: shared_lock + splice)": bug1_detected,
        "Bug 2 (Use-After-Free / Map Leak)": bug2_detected,
    }


async def main() -> None:
    print("\n" + "=" * 80)
    print("LIVE EXPERIMENT: SOLO LLM vs REVIX HARNESS ON ALTA/FastCache")
    print(f"Model: {settings.AI_MODEL_MAP} (via OpenRouter)")
    print("=" * 80)

    ai_service = AIService()

    # 1. Run Solo LLM
    print("\n[1/2] 🤖 Running Method 1: Solo LLM (Generic Single-Prompt Baseline)...")
    solo_output, solo_time = await run_solo_llm(ai_service)
    solo_checks = check_detected_bugs(solo_output)

    # 2. Run Revix Swarm Harness
    print("[2/2] 🐝 Running Method 2: Revix Swarm Harness (Tree-sitter + Multi-Agent)...")
    swarm_result, swarm_time = await run_revix_swarm(ai_service)
    swarm_text = swarm_result.summary + "\n" + "\n".join(f"{c.severity} in {c.path}:{c.line} - {c.body}" for c in swarm_result.comments)
    swarm_checks = check_detected_bugs(swarm_text)

    # 3. Print Results Comparison
    print("\n" + "=" * 80)
    print("HEAD-TO-HEAD COMPARISON RESULTS")
    print("=" * 80)

    print(f"\n--- 1. SOLO LLM (Latency: {solo_time:.2f}s) ---")
    print("Review Snippet:\n" + "\n".join(solo_output.strip().split("\n")[:12]) + "\n...")

    print(f"\n--- 2. REVIX SWARM HARNESS (Latency: {swarm_time:.2f}s, Quality Score: {swarm_result.score}/100) ---")
    print(f"Global Summary: {swarm_result.summary}")
    print(f"Actionable Comments Generated: {len(swarm_result.comments)}")
    for i, comment in enumerate(swarm_result.comments, 1):
        print(f"  [{i}] [{comment.severity}] {comment.path}:{comment.line} -> {comment.body}")
        if comment.suggested_fix:
            print(f"      Suggested Fix: {comment.suggested_fix[:80]}...")

    print("\n" + "=" * 80)
    print("DEFECT DETECTION SCOREBOARD")
    print("=" * 80)
    print(f"{'Target Critical Defect':<50} {'Solo LLM':<15} {'Revix Swarm':<15}")
    print("-" * 80)

    solo_score = sum(1 for v in solo_checks.values() if v)
    swarm_score = sum(1 for v in swarm_checks.values() if v)

    for bug_name in solo_checks:
        solo_status = "✅ CAUGHT" if solo_checks[bug_name] else "❌ MISSED"
        swarm_status = "✅ CAUGHT" if swarm_checks[bug_name] else "❌ MISSED"
        print(f"{bug_name:<50} {solo_status:<15} {swarm_status:<15}")

    print("-" * 80)
    print(f"Defect Detection Score: Solo LLM = {solo_score}/2 ({solo_score/2:.0%}) | Revix Swarm = {swarm_score}/2 ({swarm_score/2:.0%})")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
