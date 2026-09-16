# Diff-scoped AST chunking

The claim "Tree-sitter chunking scopes agent context to the syntax blocks a PR
touches" was, until this change, false in production. This is what was wrong,
how it was found, and what it now does.

## The defect

GitHub's `GET /pulls/{n}/files` returns a `patch` field — a unified diff — and
no file contents. The chunker consumed it directly:

```python
content = file_info.get("content") or file_info.get("patch", "")
chunks.extend(self._chunk_by_ast(filename, content))
```

Nothing ever populated `content`, so tree-sitter was always parsing a diff.

A diff is not source. It carries `@@` hunk headers and `+`/`-` column markers,
so the parse fails. Tree-sitter's error recovery does not fail loudly — it
returns a tree with `ERROR` nodes interleaved with whatever it could salvage.
The chunker then reassembled node text with `"\n".join(...)`, discarding the
original inter-node bytes: the indentation and the diff markers.

The output was worse than an unchunked file. Given this patch:

```diff
@@ -10,7 +10,9 @@ class PaymentProcessor:
     def __init__(self, gateway):
         self.gateway = gateway
 
-    def charge(self, amount):
-        return self.gateway.send(amount)
+    def charge(self, amount, currency="USD"):
+        if amount <= 0:
+            raise ValueError("amount must be positive")
+        return self.gateway.send(amount, currency)
```

agents received:

```text
FILE: payments.py
@@ -10,7 +10,9 @@
class PaymentProcessor:
     def __init__(self, gateway):
         self.gateway = gateway
-
def charge(self, amount):
-
return self.gateway.send(amount)
+
def charge(self, amount, currency="USD"):
...
```

Three separate failures in one artifact:

1. **No scoping.** One chunk for the whole file — the feature did not run.
2. **Corrupted structure.** `charge` and `refund` are de-indented to module
   level. An agent reasoning about `self` is reading a lie: the methods no
   longer belong to `PaymentProcessor`.
3. **Both versions present.** The pre- and post-change bodies appear as sibling
   definitions, so the agent cannot tell which one is the proposed code.

The same `patch` was also being fed to `index_file()`, so the persisted call
graph was built from these phantom symbols.

## How it surfaced

Not from a failing test — every test passed, because they all fed
`_chunk_by_ast` well-formed source, which is the one input production never
supplied. It surfaced from asking what the *type* of the value flowing in
actually was, and following `content` back to find that no writer existed.

The lesson worth keeping: error-tolerant parsers convert a crash into a wrong
answer. Tree-sitter reported `has_error == True` on every production call and
nothing was checking it.

## The fix

Parse the complete file; use the diff only to decide which nodes are in scope.

1. `GitHubService.fetch_file_content()` retrieves each changed file at the PR
   head SHA (bounded: 400 KB, 20 files, binaries and misses skipped).
2. `parse_changed_lines()` walks the hunk headers and maps the diff to
   new-file line numbers. Additions map to the line they create; deletions map
   to the line they sit against, so removing code still selects its enclosing
   node.
3. `_chunk_changed_nodes()` walks the AST of the real file and keeps only
   nodes intersecting those lines. A class larger than the chunk budget is
   descended into so a one-method edit does not drag the whole class along,
   with the class header repeated as context.
4. Selected nodes are **packed** up to the chunk budget instead of emitted one
   per call. Each call re-sends the system prompt and PR intent, so unpacked
   emission made scoping cost *more* than the full-file baseline on small
   files — measured at −224% in the worst case before packing.

Same input, after:

```text
FILE: payments.py
class PaymentProcessor:
    def __init__(self, gateway):
        self.gateway = gateway

    def charge(self, amount, currency="USD"):
        if amount <= 0:
            raise ValueError("amount must be positive")
        return self.gateway.send(amount, currency)
```

Valid Python, correct nesting, one version of the code, and unrelated classes
in the file excluded.

## Guardrails

`_chunk_changed_nodes` refuses to scope a tree with `has_error` set and falls
back to unscoped chunking. If a patch is ever passed as `source` again, the
result is a degraded review rather than a silently corrupted one.

Regression tests in [`tests/test_ast_parsing.py`](../tests/test_ast_parsing.py)
pin the behaviour that was broken:

| Test | Asserts |
| :--- | :--- |
| `test_diff_scoped_chunks_are_syntactically_valid` | every chunk `compile()`s |
| `test_diff_scoped_chunking_preserves_method_indentation` | methods stay inside their class |
| `test_diff_scoped_chunking_excludes_untouched_nodes` | untouched classes are absent |
| `test_unparseable_source_falls_back_instead_of_scoping` | a patch as source degrades, not mangles |
| `test_parse_changed_lines_records_deletions_at_their_position` | deletions select their enclosing node |

## What it is worth

Measured at **49.2%** fewer prompt tokens across 262 files from `psf/requests`
and `pallets/flask`, strongly dependent on how much of the file the diff
touches. Full methodology, the per-bucket breakdown, and live validation
against the provider's billed `usage.prompt_tokens` are in
[`docs/TOKENS.md`](TOKENS.md).
