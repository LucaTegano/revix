# Swarm Cognitive Engine: Beyond Basic RAG

Revix abandons the standard Vector RAG approach for code, as it often fails to preserve relational structures like scope, imports, and cross-file dependencies. Instead, it employs a **Multi-Agent Swarm** guided by syntactic analysis.

## 1. The Coordinator (Intelligent Routing)

The Coordinator is the "brain" of the operation. When a PR arrives:
1. It retrieves the full PR context (Title, Body, Labels).
2. It uses **Tree-sitter** to map the diff to a Concrete Syntax Tree (CST).
3. It identifies the logical boundaries of every change (e.g., "This change affects the `claim_job` method in `queue_repo.py`").
4. It uses an LLM call to decide which specialized agents are required for each chunk.

## 2. Specialized Agents & Parallel Execution

By isolating concerns, Revix minimizes "Context Pollution" and allows each agent to use specialized system prompts and tools.

- **Review Agent:** Evaluates pure logic and edge cases.
- **Security Agent:** Scans for vulnerabilities (OWASP Top 10) and network anomalies.
- **Performance Agent:** Identifies $O(n^2)$ complexities and memory allocation spikes.
- **Planning Agent:** Performs "Shift-Left" by comparing the real code against the requirements expressed in the PR description.

## 3. The Verification Agent (gVisor Sandbox)

The Verification Agent is the most proactive element. It does not just "read" code; it **tests** it.
- It generates tailored Python/Shell scripts to exercise the modified logic.
- It executes these scripts in a **cloud-isolated gVisor sandbox** (`docker run --runtime=runsc`).
- The sandbox has no network access and limited resources, ensuring untrusted code cannot compromise the worker.
- The results of the execution (stdout/stderr/exit codes) are fed back into the agent's reasoning loop to confirm or refute findings.

## 4. JSON Coercion & Integration

To ensure the system is "not a toy," all agents are forced to output rigidly typed JSON.
- Every defect includes: `path`, `line`, `side`, `severity`, and `suggested_fix`.
- These coordinates are mathematically mapped to the GitHub Pull Request API, allowing for precise, automated feedback without human intervention.
