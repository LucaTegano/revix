import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import tree_sitter_go as tsgo
import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
from litellm import acompletion
from opentelemetry import trace
from pydantic import BaseModel, Field
from tree_sitter import Language, Parser

from app.config import settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# --- Tree-sitter Setup ---
LANGUAGES = {
    ".py": Language(tspython.language()),
    ".js": Language(tsjavascript.language()),
    ".ts": Language(tsjavascript.language()),
    ".go": Language(tsgo.language()),
}

BOUNDARY_TYPES = {
    ".py": {"class_definition", "function_definition", "decorated_definition"},
    ".js": {
        "class_declaration",
        "function_declaration",
        "arrow_function",
        "method_definition",
        "export_statement",
    },
    ".go": {"function_declaration", "method_declaration", "type_declaration"},
}

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def estimate_tokens(text: str) -> int:
    """Rough token estimate used only for chunk-budget decisions, never reported."""
    return len(text) // 4


def parse_changed_lines(patch: str) -> set[int]:
    """Returns the set of new-file line numbers a unified diff touches.

    Additions map to the line they create. Deletions map to the line they sit
    against in the new file, so that removing code still selects the enclosing
    syntax node.
    """
    changed: set[int] = set()
    line_no = 0
    for raw in patch.splitlines():
        header = HUNK_HEADER_RE.match(raw)
        if header:
            line_no = int(header.group(1))
            continue
        if not raw:
            continue
        marker = raw[0]
        if marker == "+":
            changed.add(line_no)
            line_no += 1
        elif marker == "-":
            changed.add(line_no)
        elif marker == " ":
            line_no += 1
        # "\ No newline at end of file" and anything else: ignore
    return changed



def extract_json_payload(content: str) -> Any:
    content_cleaned = content.strip()
    md_match = re.search(r"```json\s*(.*?)\s*```", content_cleaned, re.DOTALL)
    if md_match:
        content_cleaned = md_match.group(1)
    else:
        content_cleaned = content_cleaned.replace("```", "")
        json_match = re.search(r"(\[.*\]|\{.*\})", content_cleaned, re.DOTALL)
        if json_match:
            content_cleaned = json_match.group(1)
    return json.loads(content_cleaned)


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            return parsed
    msg = "Tool arguments must be a JSON object"
    raise ValueError(msg)


@dataclass
class Symbol:
    name: str
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str
    parent: str | None = None


@dataclass
class Reference:
    symbol_name: str
    file_path: str
    line: int
    context_line: str
    ref_type: str


class RepoIndexer:
    def get_parser(self, file_extension: str) -> Parser | None:
        lang = LANGUAGES.get(file_extension)
        if not lang:
            return None
        return Parser(lang)

    def index_file(self, file_path: str, source: str) -> tuple[list[Symbol], list[Reference]]:
        ext = "." + file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        parser = self.get_parser(ext)
        if not parser:
            return [], []

        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)

        symbols: list[Symbol] = []
        references: list[Reference] = []

        self._extract_definitions(tree.root_node, source_bytes, file_path, symbols, ext)
        self._extract_references(tree.root_node, source_bytes, file_path, references)

        return symbols, references

    def _extract_definitions(
        self,
        node: Any,
        source_bytes: bytes,
        file_path: str,
        symbols: list[Symbol],
        ext: str,
        parent_name: str | None = None,
    ) -> None:
        boundary_types = BOUNDARY_TYPES.get(ext, set())
        if node.type in boundary_types:
            name = ""
            for child in node.children:
                if child.type in ("identifier", "name", "property_identifier", "field_identifier"):
                    name = source_bytes[child.start_byte : child.end_byte].decode("utf-8")
                    break
            if name:
                sig = (
                    source_bytes[node.start_byte : node.end_byte]
                    .decode("utf-8")
                    .split("\n")[0]
                    .strip()
                )
                symbols.append(
                    Symbol(
                        name=name,
                        qualified_name=f"{parent_name}.{name}" if parent_name else name,
                        kind=node.type,
                        file_path=file_path,
                        start_line=node.start_point[0],
                        end_line=node.end_point[0],
                        signature=sig,
                        parent=parent_name,
                    )
                )
                if node.type in ("class_definition", "class_declaration"):
                    for child in node.children:
                        self._extract_definitions(
                            child, source_bytes, file_path, symbols, ext, parent_name=name
                        )
                    return
        for child in node.children:
            self._extract_definitions(child, source_bytes, file_path, symbols, ext, parent_name)

    def _extract_references(
        self, node: Any, source_bytes: bytes, file_path: str, references: list[Reference]
    ) -> None:
        if node.type in ("call", "call_expression"):
            func_node = node.child_by_field_name("function") or (
                node.children[0] if node.children else None
            )
            if func_node:
                func_text = source_bytes[func_node.start_byte : func_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                name = func_text.split(".")[-1].split("(")[0].strip()
                if name:
                    references.append(
                        Reference(
                            symbol_name=name,
                            file_path=file_path,
                            line=node.start_point[0],
                            context_line="",
                            ref_type="call",
                        )
                    )
        for child in node.children:
            self._extract_references(child, source_bytes, file_path, references)


class ReviewComment(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    side: str = Field(default="RIGHT", pattern="^(LEFT|RIGHT)$")
    body: str
    severity: str = Field(default="INFO", pattern="^(INFO|WARNING|CRITICAL)$")
    suggested_fix: str | None = None
    agent_id: str | None = None


class ReviewResult(BaseModel):
    summary: str
    score: int = Field(ge=0, le=100)
    comments: list[ReviewComment] = Field(default_factory=list)
    agent_reports: dict[str, Any] = Field(default_factory=dict)


class AIService:
    # --- Agent Prompts ---
    # Without an explicit language contract the model answers in whatever
    # language it infers from the diff, which produced non-English reviews on
    # real PRs. Every reviewer-facing prompt pins English.
    LANGUAGE_DIRECTIVE = "Write every user-facing string in English, regardless of the language used in the code or its comments. "

    CODE_REVIEW_AGENT_PROMPT = (
        LANGUAGE_DIRECTIVE
        + "You are Revix's Code Review Agent. Review each change for correctness, security, "
        "performance, data integrity, production behavior, and alignment with the PR intent. "
        "Report only actionable defects; do not report style nitpicks, vague refactors, or "
        "duplicate concerns. Keep findings concise. You may use run_in_sandbox when executing "
        "a small standalone Python probe can validate a concrete high-risk behavior. "
        "Submit the final result using the structured submit_review tool."
    )

    MAX_CHUNK_TOKENS = 28_000

    def __init__(self) -> None:
        self.indexer = RepoIndexer()
        self.semaphore = asyncio.Semaphore(settings.AI_CONCURRENCY)

    async def _run_in_sandbox(self, script_content: str) -> dict[str, Any]:
        """Executes code in an isolated gVisor sandbox via Docker."""
        # Note: Requires gVisor (runsc) installed on the host
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w") as tmp:
            tmp.write(script_content)
            tmp.flush()

            cmd = [
                "docker",
                "run",
                "--rm",
                "--runtime=runsc",
                "--network=none",
                "-v",
                f"{tmp.name}:/app/test.py:ro",
                "python:3.11-slim",
                "python",
                "/app/test.py",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()

                # If gVisor is missing, Docker returns exit code 125 (usually) or a specific error message
                if proc.returncode == 125 or "Unknown runtime" in stderr.decode():
                    logger.warning(
                        "gVisor (runsc) not found. Falling back to standard docker runtime."
                    )
                    cmd.remove("--runtime=runsc")
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()

                return {
                    "exit_code": proc.returncode,
                    "stdout": stdout.decode(),
                    "stderr": stderr.decode(),
                }
            except Exception as e:
                logger.error("Sandbox execution failed: %s", e)
                return {"error": str(e)}

    def _get_completion_kwargs(self, model: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": settings.AI_TEMPERATURE,
            "max_tokens": settings.AI_MAX_TOKENS,
            "num_retries": 3,
        }
        if model.startswith("openrouter/"):
            kwargs["api_base"] = "https://openrouter.ai/api/v1"
            kwargs["extra_headers"] = {
                "HTTP-Referer": "https://revix.dev",
                "X-Title": settings.PROJECT_NAME,
            }
        elif settings.AI_API_BASE:
            kwargs["api_base"] = settings.AI_API_BASE

        if settings.AI_API_KEY:
            kwargs["api_key"] = settings.AI_API_KEY
        if settings.AI_FALLBACK_MODELS:
            kwargs["fallbacks"] = settings.AI_FALLBACK_MODELS
        return kwargs

    def _chunk_by_ast(self, file_path: str, source: str, patch: str | None = None) -> list[str]:
        """Splits a file into logically bound chunks using tree-sitter.

        When ``patch`` is supplied *and* ``source`` is the full file, chunking is
        scoped to the syntax nodes the diff actually touches. Without a patch the
        whole file is chunked at boundary nodes (used by tests and by callers that
        have no diff context).
        """
        ext = "." + file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        parser = self.indexer.get_parser(ext)
        if not parser:
            return [f"FILE: {file_path}\n{source}"]

        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)
        boundary_types = BOUNDARY_TYPES.get(ext, set())

        if patch:
            changed = parse_changed_lines(patch)
            if changed:
                scoped = self._chunk_changed_nodes(
                    file_path, tree, source_bytes, boundary_types, changed
                )
                if scoped:
                    return scoped

        chunks = []
        current_chunk = [f"FILE: {file_path}"]
        current_tokens = 0

        for node in tree.root_node.children:
            node_text = source_bytes[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )
            node_tokens = estimate_tokens(node_text)

            if node.type in boundary_types:
                if node_tokens > self.MAX_CHUNK_TOKENS:
                    # Case 1: Monolithic node. Try to split by children (e.g. methods in a class)
                    sub_nodes_found = False
                    if node.type in ("class_definition", "class_declaration"):
                        # Look for methods in the body
                        body = node.child_by_field_name("body")
                        if body:
                            for sub in body.children:
                                if sub.type in boundary_types:
                                    chunks.append(
                                        f"FILE: {file_path}\n"
                                        + source_bytes[sub.start_byte : sub.end_byte].decode(
                                            "utf-8", errors="replace"
                                        )
                                    )
                                    sub_nodes_found = True

                    if not sub_nodes_found:
                        # Case 2: Monolithic function or leaf that is just too big.
                        # Do NOT textual slice. Flag it.
                        chunks.append(
                            f"### [SKIP] {file_path} - unit {node.type} too large for review ({node_tokens} tokens)"
                        )
                        logger.warning("Skipping monolithic %s unit in %s", node.type, file_path)
                    continue

                if current_tokens + node_tokens > self.MAX_CHUNK_TOKENS:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = [f"FILE: {file_path}", node_text]
                    current_tokens = node_tokens
                else:
                    current_chunk.append(node_text)
                    current_tokens += node_tokens
            else:
                current_chunk.append(node_text)
                current_tokens += node_tokens

        if len(current_chunk) > 1:  # If more than just the header
            chunks.append("\n".join(current_chunk))

        return chunks

    def _chunk_changed_nodes(
        self,
        file_path: str,
        tree: Any,
        source_bytes: bytes,
        boundary_types: set[str],
        changed: set[int],
    ) -> list[str]:
        """Emits one chunk per top-level syntax node the diff touches.

        A class whose body is larger than the chunk budget is descended into so
        that an edit to a single method does not drag the whole class along; the
        class header is kept as context so the agent still knows the receiver.
        """
        if tree.root_node.has_error:
            # `source` was not a parseable file (most likely a raw patch was
            # passed in). Scoping would silently emit corrupted code, so bail
            # out and let the caller fall back to unscoped chunking.
            logger.warning(
                "Skipping diff-scoped chunking for %s: source has parse errors", file_path
            )
            return []

        def touched(node: Any) -> bool:
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            return any(start <= line <= end for line in changed)

        # Touched nodes are packed up to the chunk budget rather than emitted
        # one-per-node. Each chunk becomes a separate agent call carrying its own
        # system prompt and PR intent, so unpacked emission makes scoping cost
        # *more* than the full-file baseline on small files.
        chunks: list[str] = []
        pending: list[str] = []
        pending_tokens = 0

        def flush() -> None:
            nonlocal pending, pending_tokens
            if pending:
                chunks.append(f"FILE: {file_path}\n" + "\n\n".join(pending))
                pending = []
                pending_tokens = 0

        def add(text: str) -> None:
            nonlocal pending_tokens
            tokens = estimate_tokens(text)
            if pending and pending_tokens + tokens > self.MAX_CHUNK_TOKENS:
                flush()
            pending.append(text)
            pending_tokens += tokens

        for node in tree.root_node.children:
            if not touched(node):
                continue

            node_text = source_bytes[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )

            if (
                node.type in ("class_definition", "class_declaration")
                and estimate_tokens(node_text) > self.MAX_CHUNK_TOKENS
            ):
                body = node.child_by_field_name("body")
                if body:
                    header = (
                        source_bytes[node.start_byte : body.start_byte]
                        .decode("utf-8", errors="replace")
                        .rstrip()
                    )
                    emitted = False
                    for sub in body.children:
                        if sub.type in boundary_types and touched(sub):
                            sub_text = source_bytes[sub.start_byte : sub.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                            # Header repeated so the agent knows the receiver.
                            add(f"{header}\n{sub_text}")
                            emitted = True
                    if emitted:
                        continue

            if estimate_tokens(node_text) > self.MAX_CHUNK_TOKENS:
                flush()
                chunks.append(
                    f"### [SKIP] {file_path} - unit {node.type} too large for review "
                    f"({estimate_tokens(node_text)} tokens)"
                )
                continue

            add(node_text)

        flush()
        return chunks

    def _is_reviewable_file(self, file_path: str) -> bool:
        ignored_suffixes = (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".lock",
            ".md",
            ".txt",
            ".map",
        )
        ignored_parts = ("/node_modules/", "/dist/", "/build/", "/coverage/")
        normalized = f"/{file_path}"
        return not file_path.endswith(ignored_suffixes) and not any(
            part in normalized for part in ignored_parts
        )

    def _file_review_priority(self, pr_file: dict[str, Any]) -> int:
        filename = str(pr_file.get("filename", ""))
        patch = str(pr_file.get("patch") or pr_file.get("content") or "")
        text = f"{filename}\n{patch}".lower()

        score = min(len(patch) // 500, 8)
        high_risk_terms = (
            "auth",
            "token",
            "secret",
            "password",
            "permission",
            "admin",
            "payment",
            "webhook",
            "localstorage",
            "firebase",
            "sql",
            "database",
            "api/",
            "route",
        )
        score += sum(4 for term in high_risk_terms if term in text)
        if filename.endswith(
            (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".cpp", ".c", ".h", ".rs")
        ):
            score += 3
        return score

    def _build_review_chunks(self, pr_files: list[dict[str, Any]]) -> list[str]:
        ranked_files = sorted(
            (f for f in pr_files if self._is_reviewable_file(str(f.get("filename", "")))),
            key=self._file_review_priority,
            reverse=True,
        )

        chunks: list[str] = []
        max_chunks = max(settings.REVIEW_MAX_CHUNKS, 1)
        for file_info in ranked_files:
            filename = str(file_info["filename"])
            full_source = file_info.get("content")
            patch = file_info.get("patch") or ""

            if full_source:
                # Preferred path: parse the complete file so the AST is valid, and
                # use the patch only to decide which syntax nodes are in scope.
                produced = self._chunk_by_ast(filename, str(full_source), patch=str(patch))
            elif patch:
                # No file content available (fetch failed, file too large, binary).
                # The patch is not parseable source, so chunk it unscoped rather
                # than feeding tree-sitter a diff and emitting mangled code.
                produced = [f"FILE: {filename}\n{patch}"]
            else:
                continue

            chunks.extend(produced)
            if len(chunks) >= max_chunks:
                break

        if len(chunks) > max_chunks:
            logger.info(
                "Review chunk budget applied",
                extra={"selected_chunks": max_chunks, "candidate_chunks": len(chunks)},
            )
        return chunks[:max_chunks]

    async def analyze_diff(
        self,
        diff: str,
        repo_full_name: str,
        pr_files: list[dict[str, Any]],
        pr_details: dict[str, Any],
    ) -> ReviewResult:
        """Review AST-scoped chunks with one tool-using agent per chunk."""
        with tracer.start_as_current_span("ai.analyze_diff"):
            intent = f"TITLE: {pr_details.get('title')}\nBODY: {pr_details.get('body')}"
            chunks = self._build_review_chunks(pr_files)
            if not chunks:
                return ReviewResult(summary="No reviewable changes found.", score=100)

            results = await asyncio.gather(
                *(self._execute_review_agent(chunk, intent) for chunk in chunks)
            )
            valid_results = [result for result in results if result is not None]
            if not valid_results:
                return ReviewResult(summary="Review could not be completed.", score=0)

            return self._merge_results(valid_results)

    async def _execute_review_agent(
        self, chunk: str, intent: str
    ) -> ReviewResult | None:
        return await self._analyze_chunk_with_agent(
            "CodeReviewAgent", self.CODE_REVIEW_AGENT_PROMPT, chunk, intent
        )

    def _merge_results(self, results: list[ReviewResult]) -> ReviewResult:
        """Merge chunk reviews without another LLM call."""
        severity_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
        seen: set[tuple[str, int, str]] = set()
        comments: list[ReviewComment] = []

        for result in results:
            for comment in result.comments:
                key = (comment.path, comment.line, comment.body.strip().lower())
                if key not in seen:
                    seen.add(key)
                    comments.append(comment)

        comments.sort(key=lambda c: (severity_rank.get(c.severity, 99), c.path, c.line))
        critical = sum(c.severity == "CRITICAL" for c in comments)
        warnings = sum(c.severity == "WARNING" for c in comments)
        info = sum(c.severity == "INFO" for c in comments)

        if comments:
            summary = (
                f"Revix found {len(comments)} actionable issue(s): "
                f"{critical} critical, {warnings} warning, {info} info."
            )
        else:
            summary = "No actionable issues found."

        return ReviewResult(
            summary=summary,
            score=min(result.score for result in results),
            comments=comments,
        )

    async def _analyze_chunk_with_agent(
        self, agent_id: str, system_prompt: str, chunk: str, intent: str
    ) -> ReviewResult | None:
        with tracer.start_as_current_span(f"ai.agent_{agent_id}"):
            prompt = f"PR INTENT:\n{intent}\n\nCODE:\n{chunk}"

            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_review",
                        "description": "Submit code review feedback",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                                "comments": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "path": {"type": "string"},
                                            "line": {"type": "integer"},
                                            "side": {"type": "string", "enum": ["LEFT", "RIGHT"]},
                                            "body": {"type": "string"},
                                            "severity": {
                                                "type": "string",
                                                "enum": ["INFO", "WARNING", "CRITICAL"],
                                            },
                                            "suggested_fix": {"type": "string"},
                                        },
                                        "required": ["path", "line", "body", "side", "severity"],
                                    },
                                },
                            },
                            "required": ["summary", "score", "comments"],
                        },
                    },
                }
            ]

            tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": "run_in_sandbox",
                            "description": "Run Python code in a gVisor sandbox",
                            "parameters": {
                                "type": "object",
                                "properties": {"script": {"type": "string"}},
                                "required": ["script"],
                            },
                        },
                    }
            )

            try:
                kwargs = self._get_completion_kwargs(settings.AI_MODEL_MAP)
                kwargs.update(
                    {
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        "tools": tools,
                        "tool_choice": "auto",
                    }
                )

                async with self.semaphore:
                    response = await acompletion(**kwargs)
                message = response.choices[0].message

                if message.tool_calls:
                    for tool_call in message.tool_calls:
                        if tool_call.function.name == "run_in_sandbox":
                            script = parse_tool_arguments(tool_call.function.arguments)["script"]
                            sandbox_res = await self._run_in_sandbox(script)
                            # Feed sandbox result back to LLM
                            kwargs["messages"].append(message)
                            kwargs["messages"].append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "run_in_sandbox",
                                    "content": json.dumps(sandbox_res),
                                }
                            )
                            async with self.semaphore:
                                response = await acompletion(**kwargs)
                            message = response.choices[0].message

                    if message.tool_calls:
                        for tc in message.tool_calls:
                            if tc.function.name == "submit_review":
                                res = ReviewResult.model_validate(
                                    parse_tool_arguments(tc.function.arguments)
                                )
                                for c in res.comments:
                                    c.agent_id = agent_id
                                return res
                if message.content:
                    try:
                        data = extract_json_payload(message.content)
                        res = ReviewResult.model_validate(data)
                        for c in res.comments:
                            c.agent_id = agent_id
                        return res
                    except Exception:
                        logger.warning(
                            "Could not parse agent fallback JSON from text: %s", message.content
                        )
                return None
            except Exception:
                logger.exception("Agent %s failed", agent_id)
                return None
