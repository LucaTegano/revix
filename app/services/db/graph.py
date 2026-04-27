import logging
from typing import Any, cast

from psycopg.rows import dict_row

from app.services.db.core import db_core

logger = logging.getLogger(__name__)

class GraphRepository:
    async def persist_repo_graph(
        self, repo_full_name: str, symbols: list[dict[str, Any]], references: list[dict[str, Any]]
    ) -> None:
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM repo_symbols WHERE repo_full_name = %s",
                        (repo_full_name,)
                    )
                    await cur.execute(
                        "DELETE FROM repo_references WHERE repo_full_name = %s",
                        (repo_full_name,)
                    )

                    if symbols:
                        async with cur.copy(
                            "COPY repo_symbols (repo_full_name, name, qualified_name, kind, file_path, start_line, end_line, signature, parent) FROM STDIN"
                        ) as copy:
                            for s in symbols:
                                await copy.write_row((
                                    repo_full_name, s['name'], s['qualified_name'], s['kind'],
                                    s['file_path'], s['start_line'], s['end_line'],
                                    s['signature'], s.get('parent')
                                ))

                    if references:
                        async with cur.copy(
                            "COPY repo_references (repo_full_name, symbol_name, file_path, line, context_line, ref_type) FROM STDIN"
                        ) as copy:
                            for r in references:
                                await copy.write_row((
                                    repo_full_name, r['symbol_name'], r['file_path'],
                                    r['line'], r['context_line'], r['ref_type']
                                ))

    async def get_external_callers(
        self, repo_full_name: str, symbol_names: list[str]
    ) -> list[dict[str, Any]]:
        pool = db_core.get_pool()
        query = """
            SELECT r.*, s.file_path as defining_file
            FROM repo_references r
            JOIN repo_symbols s
              ON r.symbol_name = s.name AND r.repo_full_name = s.repo_full_name
            WHERE r.repo_full_name = %s
            AND r.symbol_name = ANY(%s)
            AND r.file_path != s.file_path
        """
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(query, (repo_full_name, symbol_names))
                    rows = await cur.fetchall()
                    return cast(list[dict[str, Any]], rows)

graph_repo = GraphRepository()
