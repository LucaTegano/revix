import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from litellm import acompletion

from app.config import settings
from app.services.ai import AIService, ReviewResult

# --- Configuration ---
DATASET_URL = "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/test.jsonl"
CACHE_FILE = "swe_bench_lite.jsonl"
DEFAULT_N = 10  # Start small, can be increased via CLI


class SWEBenchBenchmark:
    def __init__(self, n: int = DEFAULT_N):
        self.n = n
        self.ai_service = AIService()
        self.results = []

    def download_dataset(self):
        """Loads the dataset using the datasets library."""
        from datasets import load_dataset

        print("Loading SWE-bench Lite test split...")
        self.dataset = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
        print(f"Loaded {len(self.dataset)} instances.")

    def load_samples(self) -> list[dict[str, Any]]:
        indices = random.sample(range(len(self.dataset)), min(self.n, len(self.dataset)))
        return [self.dataset[i] for i in indices]

    async def judge_review(self, problem_statement: str, patch: str, review: ReviewResult) -> bool:
        """Uses an LLM to judge if the review correctly identified the issue in the patch."""
        prompt = (
            "You are a Benchmark Judge. Your task is to determine if an AI Code Review correctly identified "
            "the bug/issue described in a problem statement, given a patch that fixes it.\n\n"
            f"### PROBLEM STATEMENT:\n{problem_statement}\n\n"
            f"### THE FIX (PATCH):\n{patch}\n\n"
            f"### AI REVIEW SUMMARY:\n{review.summary}\n\n"
            f"### AI REVIEW COMMENTS:\n{json.dumps([c.dict() for c in review.comments], indent=2)}\n\n"
            "Did the AI Reviewer understand the core issue and confirm/review the fix correctly? "
            "Respond ONLY with 'SUCCESS' or 'FAILURE'."
        )
        try:
            response = await acompletion(
                model=settings.AI_MODEL_REDUCE,
                messages=[{"role": "user", "content": prompt}],
                api_key=settings.active_api_key,
                temperature=0,
            )
            content = response.choices[0].message.content.strip().upper()
            return "SUCCESS" in content
        except Exception as e:
            print(f"Judging failed: {e}")
            return False

    async def run(self):
        self.download_dataset()
        samples = self.load_samples()

        print(f"\n🚀 Running Benchmark on N={len(samples)} real-world PRs...")
        print("-" * 50)

        for i, sample in enumerate(samples):
            instance_id = sample["instance_id"]
            repo = sample["repo"]
            problem = sample["problem_statement"]
            patch = sample["patch"]

            print(f"[{i + 1}/{len(samples)}] Analyzing {instance_id} ({repo})...")

            try:
                # Prepare mock PR files for the AIService
                # SWE-bench provides a patch, we treat it as the PR diff
                pr_files = [{"filename": "fix.patch", "patch": patch}]
                pr_details = {"title": f"Fix for {instance_id}", "body": problem}

                start_time = asyncio.get_event_loop().time()
                review = await self.ai_service.analyze_diff(
                    diff=patch, repo_full_name=repo, pr_files=pr_files, pr_details=pr_details
                )
                end_time = asyncio.get_event_loop().time()

                is_success = await self.judge_review(problem, patch, review)

                self.results.append(
                    {
                        "id": instance_id,
                        "success": is_success,
                        "latency": end_time - start_time,
                        "comments_count": len(review.comments),
                    }
                )

                status = "✅ SUCCESS" if is_success else "❌ FAILURE"
                print(
                    f"      Result: {status} | Latency: {self.results[-1]['latency']:.2f}s | Comments: {len(review.comments)}"
                )

            except Exception as e:
                print(f"      💥 Error: {e}")
                self.results.append({"id": instance_id, "success": False, "error": str(e)})

            # Sleep to avoid 503/Rate limits (Free Tier is EXTREMELY sensitive)
            await asyncio.sleep(60)

        self.summarize()

    def summarize(self):
        total = len(self.results)
        successes = sum(1 for r in self.results if r.get("success"))
        avg_latency = sum(r.get("latency", 0) for r in self.results) / total if total > 0 else 0
        rate = (successes / total) * 100 if total > 0 else 0

        print("\n" + "=" * 50)
        print("FINAL SWE-BENCH LITE RESULTS")
        print("=" * 50)
        print(f"Sample Size (N):    {total}")
        print(f"Successful Aligns:  {successes}")
        print(f"Detection Rate:     {rate:.1f}%")
        print(f"Avg Latency:        {avg_latency:.2f}s")
        print("=" * 50)

        # Save results for documentation
        with open("benchmark_results.json", "w") as f:
            json.dump(
                {"n": total, "rate": rate, "avg_latency": avg_latency, "details": self.results},
                f,
                indent=2,
            )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    args = parser.parse_args()

    benchmark = SWEBenchBenchmark(n=args.n)
    asyncio.run(benchmark.run())
