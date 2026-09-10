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

VALID_AGENT_IDS = {
    "ReviewAgent",
    "SecurityAgent",
    "PerformanceAgent",
    "PlanningAgent",
    "VerificationAgent",
}


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


def normalize_agent_ids(agents: Any, chunk: str) -> list[str]:
    if isinstance(agents, dict):
        agents = agents.get("agents", ["ReviewAgent"])
    if not isinstance(agents, list):
        agents = ["ReviewAgent"]

    normalized = [agent for agent in agents if isinstance(agent, str) and agent in VALID_AGENT_IDS]
    if not normalized:
        normalized = ["ReviewAgent"]

    should_force_verification = settings.REVIEW_PROFILE != "chill"
    if (
        should_force_verification
        and ("import " in chunk or "def " in chunk)
        and "VerificationAgent" not in normalized
    ):
        normalized.append("VerificationAgent")

    return list(dict.fromkeys(normalized))


def cap_agent_ids(agents: list[str]) -> list[str]:
    if len(agents) <= settings.REVIEW_MAX_AGENTS_PER_CHUNK:
        return agents

    priority = {
        "SecurityAgent": 0,
        "ReviewAgent": 1,
        "PlanningAgent": 2,
        "PerformanceAgent": 3,
        "VerificationAgent": 4,
    }
    return sorted(agents, key=lambda agent: priority.get(agent, 99))[
        : settings.REVIEW_MAX_AGENTS_PER_CHUNK
    ]


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
    COORDINATOR_PROMPT = (
        "You are the Swarm Coordinator. Analyze the PR intent and diff. "
        "Route specific code chunks to the appropriate sub-agents: "
        "ReviewAgent (logic), SecurityAgent (vulns), PerformanceAgent (speed), "
        "VerificationAgent (execution), PlanningAgent (alignment). "
        "Return a JSON list of routing decisions."
    )

    REVIEW_AGENT_PROMPT = (
        "You are the Review Agent. Focus on logic, flow control, and edge-cases. "
        "Identify bugs and suggest fixes in JSON format. "
        "Review profile is chill: report only actionable defects likely to affect correctness, "
        "security, data integrity, or production behavior. Do not report style nitpicks, vague "
        "refactors, or duplicate concerns. Keep each finding concise."
    )

    SECURITY_AGENT_PROMPT = (
        "You are the Security Agent. Focus on vulnerabilities, injection risks, and anomalous network calls. "
        "Identify risks and suggest fixes in JSON format. Report only exploitable or realistic risks."
    )

    PERFORMANCE_AGENT_PROMPT = (
        "You are the Performance Agent. Analyze asymptotic complexity and memory allocation. "
        "Identify bottlenecks and suggest optimizations in JSON format. Only comment when the issue "
        "is measurable or likely to affect production scale."
    )

    PLANNING_AGENT_PROMPT = (
        "You are the Planning Agent. Compare the implementation with the PR intent/ticket. "
        "Ensure the changes align with the original requirements. Report only meaningful mismatches."
    )

    VERIFICATION_AGENT_PROMPT = (
        "You are the Verification Agent. Generate a standalone Python script to test the logic of the provided code. "
        "The script will be executed in a gVisor sandbox. Use the 'run_in_sandbox' tool only when "
        "execution can validate a concrete high-risk behavior."
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
        if filename.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".cpp", ".c", ".h", ".rs")):
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
            content = file_info.get("content") or file_info.get("patch", "")
            if not content:
                continue
            chunks.extend(self._chunk_by_ast(str(file_info["filename"]), str(content)))
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
        with tracer.start_as_current_span("ai.analyze_diff"):
            intent = f"TITLE: {pr_details.get('title')}\nBODY: {pr_details.get('body')}"

            # 1. AST-aware chunking with a CodeRabbit-style noise budget.
            chunks = self._build_review_chunks(pr_files)

            # 2. Coordinator Routing
            routing_tasks = [self._coordinate_routing(chunk, intent) for chunk in chunks]
            routing_decisions = await asyncio.gather(*routing_tasks)

            # 3. Swarm Execution
            agent_tasks = []
            for i, chunk in enumerate(chunks):
                decisions = cap_agent_ids(routing_decisions[i])
                for agent_id in decisions:
                    agent_tasks.append(self._execute_agent(agent_id, chunk, intent))

            if not agent_tasks:
                return ReviewResult(summary="No issues found by swarm.", score=100)

            results = await asyncio.gather(*agent_tasks)

            # 4. Synthesis
            all_comments = [c for res in results if res for c in res.comments]
            summaries = [res.summary for res in results if res]

            return await self._reduce_summaries(summaries, all_comments)

    async def _coordinate_routing(self, chunk: str, intent: str) -> list[str]:
        """Coordinator decides which agents should look at this chunk using an LLM."""
        prompt = (
            "Decide which agents should analyze this code chunk based on the PR intent.\n"
            "Use a chill review profile: choose the fewest agents needed, prefer ReviewAgent "
            "or SecurityAgent, and avoid VerificationAgent unless execution is clearly valuable.\n"
            f"PR INTENT:\n{intent}\n\n"
            f"CODE CHUNK:\n{chunk}\n\n"
            "AVAILABLE AGENTS: ReviewAgent, SecurityAgent, PerformanceAgent, VerificationAgent, PlanningAgent.\n"
            "Return only a JSON list of agent IDs."
        )
        try:
            kwargs = self._get_completion_kwargs(settings.AI_MODEL_MAP)
            kwargs.update(
                {
                    "messages": [
                        {"role": "system", "content": self.COORDINATOR_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    # Some free models struggle with response_format, we'll handle raw text too
                }
            )
            async with self.semaphore:
                response = await acompletion(**kwargs)
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from Coordinator")

            try:
                data = extract_json_payload(content)
            except json.JSONDecodeError:
                logger.warning("Failed parsing LLM JSON content directly. Raw content: %r", content)
                raise

            return cap_agent_ids(normalize_agent_ids(data, chunk))
        except Exception:
            logger.exception("Coordinator routing failed")
            return ["ReviewAgent", "SecurityAgent"]

    async def _execute_agent(self, agent_id: str, chunk: str, intent: str) -> ReviewResult | None:
        """Executes a specific agent on a chunk."""
        prompts = {
            "ReviewAgent": self.REVIEW_AGENT_PROMPT,
            "SecurityAgent": self.SECURITY_AGENT_PROMPT,
            "PerformanceAgent": self.PERFORMANCE_AGENT_PROMPT,
            "PlanningAgent": self.PLANNING_AGENT_PROMPT,
            "VerificationAgent": self.VERIFICATION_AGENT_PROMPT,
        }

        system_prompt = prompts.get(agent_id, self.REVIEW_AGENT_PROMPT)
        # Implement agent call logic similar to old _analyze_chunk but with specialized prompt
        # ... simplified for this edit ...
        return await self._analyze_chunk_with_agent(agent_id, system_prompt, chunk, intent)

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

            if agent_id == "VerificationAgent":
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
                async with self.semaphore:
                    response = await acompletion(**kwargs)
                output = parse_tool_arguments(
                    response.choices[0].message.tool_calls[0].function.arguments
                )
                return ReviewResult(
                    summary=output["global_summary"],
                    score=output["global_score"],
                    comments=comments,
                )
            except Exception as e:
                logger.warning("Structured reduce failed, trying fallback text completion: %s", e)
                try:
                    kwargs = self._get_completion_kwargs(settings.AI_MODEL_REDUCE)
                    prompt = (
                        "Synthesize these summaries into one global review.\n"
                        "You must respond with a JSON object containing:\n"
                        "- 'global_summary': a string summary of the changes\n"
                        "- 'global_score': an integer score from 0 to 100\n\n"
                        "Summaries:\n" + "\n".join(summaries)
                    )
                    kwargs.update(
                        {
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "You are a review synthesis helper. Output JSON only.",
                                },
                                {"role": "user", "content": prompt},
                            ]
                        }
                    )
                    async with self.semaphore:
                        response = await acompletion(**kwargs)
                    content = response.choices[0].message.content
                    if content:
                        data = extract_json_payload(content)
                        return ReviewResult(
                            summary=data["global_summary"],
                            score=int(data["global_score"]),
                            comments=comments,
                        )
                except Exception:
                    logger.exception("Fallback reduce phase failed")
                return ReviewResult(summary="Synthesis failed.", score=0, comments=comments)
