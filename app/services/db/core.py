import asyncio
import logging
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.config import settings

logger = logging.getLogger(__name__)

class DatabaseCore:
    def __init__(self) -> None:
        self.pool: AsyncConnectionPool[Any] | None = None

    async def connect(self) -> None:
        if not self.pool:
            # psycopg3 uses a slightly different connection string format or parameters
            # but standard postgresql:// works.
            self.pool = AsyncConnectionPool(
                conninfo=settings.DATABASE_URL,
                min_size=settings.DB_POOL_MIN_SIZE,
                max_size=settings.db_pool_max_size,
                open=False, # We will open it manually
                kwargs={
                    "prepare_threshold": None, # Disable server-side prepared statements if using PgBouncer
                }
            )
            await self.pool.open()
            logger.info("Connected to psycopg (v3) Async Pool.")

    async def wait_for_tables(self, tables: list[str], retries: int = 10, delay: int = 2) -> None:
        """Wait for specific tables to exist in the database."""
        pool = self.get_pool()
        for i in range(retries):
            try:
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        query = (
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                        await cur.execute(query)
                        rows = await cur.fetchall()
                        existing_tables = {row[0] for row in rows}

                        if all(t in existing_tables for t in tables):
                            logger.info("✅ All required tables (%s) present.", ", ".join(tables))
                            return

                        logger.warning(
                            "⏳ Tables %s not found. (Attempt %d/%d)...",
                            [t for t in tables if t not in existing_tables], i + 1, retries
                        )
            except Exception as e:
                logger.warning("⏳ Database not ready (%s). Waiting...", e)

            await asyncio.sleep(delay)

        raise RuntimeError(f"Required tables {tables} were never created.")

    async def disconnect(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    def get_pool(self) -> AsyncConnectionPool[Any]:
        if not self.pool:
            raise RuntimeError("Database pool not initialized. Call connect() first.")
        return self.pool

db_core = DatabaseCore()
