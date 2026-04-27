import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import propagate, trace

from app.worker import ReviewWorker


@pytest.fixture
def worker() -> ReviewWorker:
    return ReviewWorker()

@pytest.mark.asyncio
async def test_worker_trace_propagation(worker: ReviewWorker) -> None:
    # Create a dummy trace context
    span_context = trace.SpanContext(
        trace_id=0xDEADBEEFDEADBEEFDEADBEEFDEADBEEF,
        span_id=0xDEADBEEFDEADBEEF,
        is_remote=True,
        trace_flags=trace.TraceFlags.SAMPLED,
    )
    parent_span = trace.NonRecordingSpan(span_context)
    parent_context = trace.set_span_in_context(parent_span)
    
    carrier: dict[str, str] = {}
    propagate.inject(carrier, context=parent_context)
    
    # Mock job payload with this carrier
    job = {
        "id": "job-123",
        "commit_sha": "sha123",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "fence_token": 1,
        "payload": json.dumps({
            "installation_id": 123,
        }),
        "otel_context": json.dumps(carrier)
    }
    
    # Mock dependencies
    with patch("app.worker.GitHubService", return_value=AsyncMock()), \
         patch("app.worker.db_service", AsyncMock()), \
         patch("app.worker.queue_repo", AsyncMock()), \
         patch.object(worker.ai, "analyze_diff", new_callable=AsyncMock) as mock_analyze:

        
        mock_analyze.return_value = MagicMock()
        
        # We want to verify that worker.process_job starts a span with the correct parent
        # We can use a custom tracer to capture the context
        with patch("app.worker.tracer.start_as_current_span") as mock_start_span:
            await worker.process_job(job)
            
            # The 'context' argument to start_as_current_span should contain our trace_id
            call_args = mock_start_span.call_args
            extracted_context = call_args.kwargs.get("context")
            
            # Extract span from the context passed to start_as_current_span
            span = trace.get_current_span(extracted_context)
            assert span.get_span_context().trace_id == 0xDEADBEEFDEADBEEFDEADBEEFDEADBEEF
