import pytest

from app.services.ai import AIService, RepoIndexer, parse_changed_lines


@pytest.fixture
def indexer():
    return RepoIndexer()


@pytest.fixture
def ai_service():
    return AIService()


def test_python_ast_parsing(indexer):
    code = """
class MyClass:
    def method_one(self):
        pass

def top_level_func():
    pass
"""
    symbols, references = indexer.index_file("test.py", code)

    assert len(symbols) == 3
    names = [s.name for s in symbols]
    assert "MyClass" in names
    assert "method_one" in names
    assert "top_level_func" in names

    # Check qualified names
    method = next(s for s in symbols if s.name == "method_one")
    assert method.qualified_name == "MyClass.method_one"
    assert method.parent == "MyClass"


def test_javascript_ast_parsing(indexer):
    code = """
class User {
  constructor(name) {
    this.name = name;
  }
  sayHello() {
    console.log("Hello");
  }
}

function outer() {
  const inner = () => {};
}
"""
    symbols, references = indexer.index_file("test.js", code)
    assert len(symbols) >= 3
    names = [s.name for s in symbols]
    assert "User" in names
    assert "sayHello" in names
    assert "outer" in names


def test_go_ast_parsing(indexer):
    code = """
package main
import "fmt"

type User struct {
    Name string
}

func (u *User) GetName() string {
    return u.Name
}

func main() {
    fmt.Println("Hello")
}
"""
    symbols, references = indexer.index_file("test.go", code)
    names = [s.name for s in symbols]
    assert "GetName" in names
    assert "main" in names


def test_chunk_by_ast(ai_service):
    code = """
def func1():
    print("one")

def func2():
    print("two")

def func3():
    print("three")
"""
    # Mock small max tokens to force splitting
    ai_service.MAX_CHUNK_TOKENS = 10
    chunks = ai_service._chunk_by_ast("test.py", code)

    # Should have multiple chunks
    assert len(chunks) >= 3
    assert all("FILE: test.py" in c for c in chunks)
    assert any("func1" in c for c in chunks)
    assert any("func2" in c for c in chunks)
    assert any("func3" in c for c in chunks)


def test_chunk_by_ast_monolithic_fallback(ai_service):
    # If it's a huge monolithic function that can't be split
    code = "def huge_func():\n" + "    print('loooooong')\n" * 1000
    ai_service.MAX_CHUNK_TOKENS = 10
    chunks = ai_service._chunk_by_ast("test.py", code)

    assert len(chunks) == 1
    assert "[SKIP]" in chunks[0]
    assert "too large for review" in chunks[0]


def test_chunk_by_ast_unsupported_ext(ai_service):
    code = "some random text"
    chunks = ai_service._chunk_by_ast("test.txt", code)
    assert len(chunks) == 1
    assert "FILE: test.txt" in chunks[0]


# --- Diff-scoped chunking -------------------------------------------------
#
# Regression guard for the defect where the GitHub `patch` field was handed to
# tree-sitter directly. A unified diff is not parseable source: it yields ERROR
# nodes, and reassembling node text dropped the diff prefixes and indentation,
# so agents received code whose structure did not match the real file.


FULL_SOURCE = """import os


class PaymentProcessor:
    def __init__(self, gateway):
        self.gateway = gateway

    def charge(self, amount, currency="USD"):
        if amount <= 0:
            raise ValueError("amount must be positive")
        return self.gateway.send(amount, currency)


class AuditLog:
    def record(self, event):
        print(event)


def unrelated_helper(x):
    return x * 2
"""

PATCH = """@@ -5,6 +5,8 @@ class PaymentProcessor:
     def __init__(self, gateway):
         self.gateway = gateway
 
-    def charge(self, amount):
-        return self.gateway.send(amount)
+    def charge(self, amount, currency="USD"):
+        if amount <= 0:
+            raise ValueError("amount must be positive")
+        return self.gateway.send(amount, currency)
"""


def test_parse_changed_lines_maps_additions_to_new_file():
    changed = parse_changed_lines(PATCH)
    # Hunk starts at new-file line 5; three context lines, then the additions.
    assert changed == {8, 9, 10, 11}


def test_parse_changed_lines_records_deletions_at_their_position():
    deletion_only = "@@ -10,4 +10,2 @@\n context\n-gone_one\n-gone_two\n more\n"
    # Both deletions sit against new-file line 11, which is where the
    # enclosing syntax node must still be selected from.
    assert 11 in parse_changed_lines(deletion_only)


def test_diff_scoped_chunking_excludes_untouched_nodes(ai_service):
    chunks = ai_service._chunk_by_ast("payments.py", FULL_SOURCE, patch=PATCH)

    assert chunks, "diff-scoped chunking produced nothing"
    joined = "\n".join(chunks)
    assert "PaymentProcessor" in joined
    assert "AuditLog" not in joined
    assert "unrelated_helper" not in joined


def test_diff_scoped_chunks_are_syntactically_valid(ai_service):
    """The old implementation emitted code that would not compile."""
    chunks = ai_service._chunk_by_ast("payments.py", FULL_SOURCE, patch=PATCH)

    for chunk in chunks:
        body = chunk.split("\n", 1)[1]  # drop the "FILE: ..." header
        compile(body, "<chunk>", "exec")  # raises SyntaxError on mangled output


def test_diff_scoped_chunking_preserves_method_indentation(ai_service):
    """Methods must stay inside their class, not get hoisted to module level."""
    chunks = ai_service._chunk_by_ast("payments.py", FULL_SOURCE, patch=PATCH)
    joined = "\n".join(chunks)

    assert "    def charge(" in joined
    assert "\ndef charge(" not in joined


def test_unparseable_source_falls_back_instead_of_scoping(ai_service):
    """If a patch is ever passed as `source`, scoping must bail, not mangle."""
    chunks = ai_service._chunk_by_ast("payments.py", PATCH, patch=PATCH)

    # Falls through to unscoped chunking rather than emitting corrupted nodes.
    assert len(chunks) == 1
    assert "FILE: payments.py" in chunks[0]


def test_build_review_chunks_prefers_content_over_patch(ai_service):
    scoped = ai_service._build_review_chunks(
        [{"filename": "payments.py", "content": FULL_SOURCE, "patch": PATCH}]
    )
    assert "AuditLog" not in "\n".join(scoped)

    # Without content there is nothing parseable, so the patch is passed through
    # verbatim rather than run through tree-sitter.
    patch_only = ai_service._build_review_chunks([{"filename": "payments.py", "patch": PATCH}])
    assert len(patch_only) == 1
    assert "@@" in patch_only[0]
