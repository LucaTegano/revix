# 🤝 Contributing to LucAI

We love contributions! To maintain high code quality, please follow these guidelines.

## 🛠️ Local Development

1. **Fork and Clone** the repository.
2. **Setup with Makefile**:
   ```bash
   make install
   make db
   ```
3. **Run Tests**:
   ```bash
   make test
   ```

## 📏 Coding Standards

We enforce quality checks via `ruff`, `mypy`, and `pytest`.

### 1. Linting & Formatting
We use **Ruff** for linting and formatting. Run it before committing:
```bash
make lint
```

### 2. Type Checking
Functions should have type hints. We use **Mypy** to verify this.

### 3. Testing
- Write unit tests for new services in `tests/`.
- Ensure coverage for `GitHubService` and `AIService` abstractions.

## 📖 Documentation

Before diving into the code, please read:
- [System Design Doc](docs/explanation.md)
- [Local Setup](docs/walkthrough.md)

## 🚀 Pull Request Process

1. Create a descriptive feature branch (`feat/add-new-ai-provider`).
2. Ensure all `make lint` and `make test` checks pass locally.
3. Update the documentation if applicable.
4. Submit the PR with a clear description of the changes.

## 🐞 Reporting Issues
Use GitHub Issues to report bugs or suggest features. Provide context, including logs and steps to reproduce.

---
*Thank you for helping us improve LucAI!*
