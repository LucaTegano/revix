import pytest

from app.services.ai import AIService, RepoIndexer


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
