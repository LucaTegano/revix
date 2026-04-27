from app.services.ai import RepoIndexer

# Each tuple: (name, caller_source, symbol_name, expected_line, detectable)
CASES = [
    (
        "direct_call",
        "def foo(): pass\ndef bar():\n    foo()",
        "foo", 2, True,
    ),
    (
        "method_call",
        "class A:\n    def go(self): pass\n\na = A()\na.go()",
        "go", 4, True,
    ),
    (
        "chained_call",
        "def get_client(): pass\ndef run():\n    get_client().connect()",
        "get_client", 2, True,
    ),
    (
        "dynamic_getattr",
        "def process(): pass\ngetattr(obj, method_name)()",
        "process", None, False,  # Runtime, excluded from recall
    ),
    (
        "importlib",
        "def handler(): pass\nmod = importlib.import_module(name)",
        "handler", None, False,  # Runtime, excluded from recall
    ),
]

def test_static_recall():
    static_cases = [(name, src, sym, line) 
                    for name, src, sym, line, detectable in CASES 
                    if detectable]
    
    found = 0
    indexer = RepoIndexer()
    for name, source, symbol_name, expected_line in static_cases:
        symbols, refs = indexer.index_file(f"{name}.py", source)
        
        found_ref = any(r.symbol_name == symbol_name and r.line == expected_line for r in refs)
        
        assert found_ref, (
            f"Missed static call in '{name}': "
            f"expected {symbol_name} at line {expected_line}"
        )
        found += 1
    
    recall = found / len(static_cases)
    assert recall == 1.0
    print(f"\n✅ Static recall: {recall:.0%}")

def test_dynamic_dispatch_not_claimed():
    """
    Verify we do NOT falsely claim to detect dynamic dispatch.
    These cases are excluded from the recall metric by design.
    """
    dynamic_cases = [(name, src, sym)
                     for name, src, sym, line, detectable in CASES
                     if not detectable]
    
    indexer = RepoIndexer()
    for name, source, symbol_name in dynamic_cases:
        symbols, refs = indexer.index_file(f"{name}.py", source)
        
        # We should NOT find the symbol as a call reference
        found_ref = any(r.symbol_name == symbol_name for r in refs)
        assert not found_ref, f"Incorrectly detected dynamic call in '{name}'"

if __name__ == "__main__":
    test_static_recall()
    test_dynamic_dispatch_not_claimed()
    print("✅ All recall tests passed.")
