import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from opentelemetry import metrics, propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from pythonjsonlogger import json as jsonlogger

from app.config import get_settings
from app.services.db.core import db_core
from app.services.db.queue import queue_repo
from app.services.github import GitHubService

db_service = queue_repo

try:
    settings = get_settings()
except Exception as e:
    logging.critical("❌ Configuration error: %s", e)
    sys.exit(1)

log_handler = logging.StreamHandler(sys.stdout)
if not settings.DEBUG:
    log_handler.setFormatter(jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
logging.basicConfig(handlers=[log_handler], level=logging.INFO if not settings.DEBUG else logging.DEBUG)
logger = logging.getLogger(__name__)

# --- OpenTelemetry Setup ---
resource = Resource(attributes={SERVICE_NAME: "lucai-api"})
provider = TracerProvider(resource=resource)
processor = BatchSpanProcessor(ConsoleSpanExporter())
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    logger.info("✅ Configuration validated: %s", settings.AI_PROVIDER)
    await db_core.connect()
    metrics_task = asyncio.create_task(queue_metrics_loop())
    
    yield
    
    # Shutdown
    metrics_task.cancel()
    try:
        await metrics_task
    except asyncio.CancelledError:
        pass
    await db_core.disconnect()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)

meter = metrics.get_meter("lucai.metrics")
webhook_duration = meter.create_histogram("webhook.ingestion.duration_ms")
queue_dwell = meter.create_histogram("queue.dwell.duration_ms")

tracer = trace.get_tracer(__name__)


async def queue_metrics_loop() -> None:
    while True:
        try:
            pool = db_core.get_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
                    row_pending = await cur.fetchone()
                    pending = row_pending[0] if row_pending else 0

                    await cur.execute("SELECT COUNT(*) FROM worker_heartbeats")
                    row_active = await cur.fetchone()
                    active = row_active[0] if row_active else 0

                    logger.info("Queue metrics", extra={"queue_depth": pending, "active_workers": active})
        except Exception:
            pass
        await asyncio.sleep(15)


def verify_signature(body: bytes, signature: str) -> None:
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")
    
    expected_signature = hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(f"sha256={expected_signature}", signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.post("/api/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(None)
) -> dict[str, str]:
    start_time = time.time()
    body = await request.body()
    verify_signature(body, x_hub_signature_256)

    payload = json.loads(body)

    if x_github_event == "pull_request":
        action = payload.get("action")
        if action in ("opened", "synchronize"):
            pr = payload["pull_request"]
            sha = pr["head"]["sha"]
            repo = payload["repository"]["full_name"]
            num = pr["number"]
            inst_id = payload["installation"]["id"]

            with tracer.start_as_current_span("api.webhook_ingest") as span:
                span.set_attribute("commit_sha", sha)
                span.set_attribute("repo", repo)

                trace_context: dict[str, Any] = {}
                propagate.inject(trace_context)

                try:
                    enqueued = await asyncio.wait_for(
                        queue_repo.enqueue_if_new(
                            sha=sha, repo=repo, pull_number=num,
                            installation_id=inst_id, trace_context=trace_context
                        ),
                        timeout=3.0
                    )
                    if enqueued:
                        logger.info("Enqueued job", extra={"repo": repo, "pr_number": num, "commit_sha": sha})
                        webhook_duration.record((time.time() - start_time) * 1000)
                        return {"msg": "accepted"}
                    else:
                        webhook_duration.record((time.time() - start_time) * 1000)
                        return {"msg": "already exists"}
                except TimeoutError as err:
                    raise HTTPException(status_code=503, detail="Database busy, please retry") from err

    elif x_github_event == "issue_comment":
        action = payload.get("action")
        if action == "created":
            comment_body = payload["comment"]["body"].strip().lower()
            if comment_body in ("/lucai-ignore", "/lucai-approve", "/lucai-ok"):
                repo = payload["repository"]["full_name"]
                num = payload["issue"]["number"]
                inst_id = payload["installation"]["id"]
                
                logger.info("Override command detected", extra={"repo": repo, "pr_number": num, "command": comment_body})
                
                github = GitHubService()
                try:
                    token = await github.get_token(inst_id)
                    check_run_id = await queue_repo.get_latest_check_run_id(repo, num)
                    if check_run_id:
                        await github.update_check_run(
                            repo=repo,
                            check_run_id=check_run_id,
                            token=token,
                            conclusion="success",
                            output={
                                "title": "Manual Override: Approved",
                                "summary": f"Review status overridden by user comment: {comment_body}",
                                "text": "The quality gate has been manually bypassed."
                            }
                        )
                        logger.info("Successfully overridden check run", extra={"check_run_id": check_run_id})
                    else:
                        logger.warning("No check run found to override", extra={"repo": repo, "pr_number": num})
                except Exception as e:
                    logger.error("Failed to perform override: %s", e)
                finally:
                    await github.close()

    webhook_duration.record((time.time() - start_time) * 1000)
    return {"msg": "accepted"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
