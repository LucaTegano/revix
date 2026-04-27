from app.services.ai import RepoIndexer


class MockGraph:
    def __init__(self):
        self.symbols = {}
        self.references = {}

def test_recall_on_static_calls():
    """
    Verify Tree-sitter finds statically-resolvable call sites.
    """
    indexer = RepoIndexer()
    
    # CASE 1: Direct function call
    source_caller = """
def main():
    calculate_tax(100, 0.1)
"""
    symbols, refs = indexer.index_file("billing.py", source_caller)
    
    found_ref = any(
        r.symbol_name == "calculate_tax" and r.line == 2
        for r in refs
    )
    assert found_ref, "Failed to detect direct function call"

    # CASE 2: Method call
    source_method = """
class Service:
    def process(self, data):
        pass

s = Service()
s.process(payload)
"""
    symbols, refs = indexer.index_file("handler.py", source_method)
    
    # We expect 'process' to be a reference
    found_ref = any(
        r.symbol_name == "process" and r.line == 6
        for r in refs
    )
    assert found_ref, "Failed to detect method call"

def test_dynamic_limitations():
    """
    Document what we explicitly CANNOT find via static analysis.
    """
    indexer = RepoIndexer()
    
    source_dynamic = """
method_name = "calculate_tax"
getattr(obj, method_name)(100)
"""
    symbols, refs = indexer.index_file("dynamic.py", source_dynamic)
    
    # We should NOT find 'calculate_tax' because it's a string literal, not a call node
    found_ref = any(
        r.symbol_name == "calculate_tax"
        for r in refs
    )
    assert not found_ref, "Static analysis incorrectly claimed to resolve dynamic getattr"

if __name__ == "__main__":
    test_recall_on_static_calls()
    test_dynamic_limitations()
    print("✅ Recall tests passed (Static resolved, Dynamic correctly missed).")
