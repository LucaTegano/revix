import statistics
import time
from pathlib import Path

from app.services.ai import RepoIndexer


def benchmark_indexer():
    print("🚀 Starting Indexer Benchmark...")
    indexer = RepoIndexer()
    
    # Use the current project as a test fixture
    root_path = Path(__file__).parent.parent
    py_files = list(root_path.rglob("*.py"))
    
    # Filter out venv and cache
    py_files = [f for f in py_files if "venv" not in str(f) and ".mypy_cache" not in str(f)]
    
    if not py_files:
        print("❌ No python files found to benchmark.")
        return

    file_latencies = []
    total_symbols = 0
    total_refs = 0
    
    print(f"📊 Testing against {len(py_files)} files in the current repository...")

    for file_path in py_files:
        try:
            source = file_path.read_text(encoding='utf-8', errors='replace')
            
            start = time.perf_counter()
            symbols, refs = indexer.index_file(str(file_path), source)
            elapsed_ms = (time.perf_counter() - start) * 1000
            
            file_latencies.append(elapsed_ms)
            total_symbols += len(symbols)
            total_refs += len(refs)
        except Exception as e:
            print(f"⚠️ Failed to index {file_path}: {e}")

    if not file_latencies:
        return

    results = {
        "p50_ms": statistics.median(file_latencies),
        "p95_ms": statistics.quantiles(file_latencies, n=20)[18],
        "p99_ms": statistics.quantiles(file_latencies, n=100)[98],
        "max_ms": max(file_latencies),
        "avg_ms": statistics.mean(file_latencies),
        "total_files": len(file_latencies),
        "total_symbols": total_symbols,
        "total_refs": total_refs
    }

    print("\n📈 Benchmark Results:")
    print(f"  Files Indexed:   {results['total_files']}")
    print(f"  Total Symbols:   {results['total_symbols']}")
    print(f"  Total Refs:      {results['total_refs']}")
    print(f"  Avg Latency:     {results['avg_ms']:.2f}ms")
    print(f"  p50 Latency:     {results['p50_ms']:.2f}ms")
    print(f"  p95 Latency:     {results['p95_ms']:.2f}ms")
    print(f"  p99 Latency:     {results['p99_ms']:.2f}ms")
    print(f"  Max Latency:     {results['max_ms']:.2f}ms")
    
    return results

if __name__ == "__main__":
    benchmark_indexer()
