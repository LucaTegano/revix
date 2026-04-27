import asyncio
import json
import logging
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
from app.services.db.graph import graph_repo

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
                if child.type in ("identifier", "name"):
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
    path: str
    line: int
    side: str = Field(default="RIGHT", pattern="^(LEFT|RIGHT)$")
    body: str
    severity: str = Field(default="INFO", pattern="^(INFO|WARNING|CRITICAL)$")


class ReviewResult(BaseModel):
    summary: str
    score: int = Field(ge=0, le=100)
    comments: list[ReviewComment] = Field(default_factory=list)


class AIService:
    def __init__(self) -> None:
        self.indexer = RepoIndexer()

    SYSTEM_PROMPT = (
        "You are an expert Senior Software Engineer acting as a Quality Gatekeeper. "
        "Analyze the provided code changes (diff delta) for logic errors, security vulnerabilities, and performance issues. "
        "\n\nGUIDELINES:\n"
        "1. Focus ONLY on the changes provided in the diff (+/- lines).\n"
        "2. Provide a 'score' from 0 to 100 representing the quality of the PR (100 is perfect).\n"
        "3. For each violation, specify the 'side' (use 'RIGHT' for additions/modifications in the PR, 'LEFT' for deletions if relevant).\n"
        "4. Assign a 'severity' (INFO, WARNING, CRITICAL).\n"
        "5. IMPORTANT: Use the EXACT file path and line numbers from the diff. Do NOT guess."
    )

    MAX_CHUNK_TOKENS = 28_000

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
                "HTTP-Referer": "https://github.com/LucaTegano/revix",
                "X-Title": "Revix",
            }
        elif settings.AI_API_BASE:
            kwargs["api_base"] = settings.AI_API_BASE

        if settings.active_api_key:
            kwargs["api_key"] = settings.active_api_key
        if settings.AI_FALLBACK_MODELS:
            kwargs["fallbacks"] = settings.AI_FALLBACK_MODELS
        return kwargs

    def _chunk_by_ast(self, file_path: str, source: str) -> list[str]:
        """Splits file into logically bound chunks using tree-sitter. Attempts sub-node split for huge classes."""
        ext = "." + file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        parser = self.indexer.get_parser(ext)
        if not parser:
            return [f"FILE: {file_path}\n{source}"]

        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)
        boundary_types = BOUNDARY_TYPES.get(ext, set())

        chunks = []
        current_chunk = [f"FILE: {file_path}"]
        current_tokens = 0

        def estimate_tokens(text: str) -> int:
            return len(text) // 4

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

    async def analyze_diff(
        self, diff: str, repo_full_name: str, pr_files: list[dict[str, Any]]
    ) -> ReviewResult:
        with tracer.start_as_current_span("ai.analyze_diff") as span:
            # 1. Deterministic Context (Call Graph)
            changed_symbols = []
            for f in pr_files:
                if "content" in f:
                    syms, _ = self.indexer.index_file(f["filename"], f["content"])
                    changed_symbols.extend([s.name for s in syms])

            context_data = []
            if changed_symbols:
                external_callers = await graph_repo.get_external_callers(
                    repo_full_name, changed_symbols
                )
                for caller in external_callers[:10]:
                    context_data.append(
                        f"External Call: {caller['file_path']}:{caller['line']} calls '{caller['symbol_name']}'"
                    )

            repo_context = "\n".join(context_data)

            # 2. AST-Aware Chunking
            # We chunk the whole file content if provided, otherwise we fallback to diff chunks
            all_tasks = []
            for f in pr_files:
                content = f.get("content") or f.get("patch", "")
                file_chunks = self._chunk_by_ast(f["filename"], content)
                for i, chunk in enumerate(file_chunks):
                    all_tasks.append(self._analyze_chunk(chunk, i, repo_context))

            span.set_attribute("chunk_count", len(all_tasks))
            if not all_tasks:
                return ReviewResult(summary="No code to review.", score=100, comments=[])

            results = await asyncio.gather(*all_tasks)
            summaries = [f"Score: {res.score} | Summary: {res.summary}" for res in results if res]
            all_comments = [c for res in results if res for c in res.comments]

            if not summaries:
                return ReviewResult(summary="Analysis failed.", score=0)

            if len(results) == 1:
                single_res = results[0]
                if single_res:
                    return single_res
                return ReviewResult(summary="Analysis failed for single chunk.", score=0)

            return await self._reduce_summaries(summaries, all_comments)

    async def _analyze_chunk(
        self, chunk: str, index: int, repo_context: str
    ) -> ReviewResult | None:
        with tracer.start_as_current_span(f"ai.map_chunk_{index}"):
            prompt = "Review this code block.\n"
            if repo_context:
                prompt += f"\nREPO CONTEXT (EXTERNAL CALLERS):\n{repo_context}\n"
            prompt += f"\nCODE:\n{chunk}"

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
            try:
                kwargs = self._get_completion_kwargs(settings.AI_MODEL_MAP)
                kwargs.update(
                    {
                        "messages": [
                            {"role": "system", "content": self.SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "tools": tools,
                        "tool_choice": "required",
                    }
                )
                response = await acompletion(**kwargs)
                args = response.choices[0].message.tool_calls[0].function.arguments
                return ReviewResult.model_validate(json.loads(args))
            except Exception:
                logger.exception("Map chunk %d failed", index)
                return None

    async def _reduce_summaries(
        self, summaries: list[str], comments: list[ReviewComment]
    ) -> ReviewResult:
        with tracer.start_as_current_span("ai.reduce_summaries"):
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "synthesize_review",
                        "description": "Synthesize multiple review chunks",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "global_summary": {"type": "string"},
                                "global_score": {"type": "integer", "minimum": 0, "maximum": 100},
                            },
                            "required": ["global_summary", "global_score"],
                        },
                    },
                }
            ]
            try:
                kwargs = self._get_completion_kwargs(settings.AI_MODEL_REDUCE)
                kwargs.update(
                    {
                        "messages": [
                            {
                                "role": "system",
                                "content": "Synthesize these summaries into one global review.",
                            },
                            {"role": "user", "content": "Summaries:\n" + "\n".join(summaries)},
                        ],
                        "tools": tools,
                        "tool_choice": "required",
                    }
                )
                response = await acompletion(**kwargs)
                output = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
                return ReviewResult(
                    summary=output["global_summary"],
                    score=output["global_score"],
                    comments=comments,
                )
            except Exception:
                logger.exception("Reduce phase failed")
                return ReviewResult(summary="Synthesis failed.", score=0, comments=comments)
