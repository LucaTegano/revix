from typing import Any

from app.services.db.core import db_core
from app.services.db.queue import JobQueueRepository


class DatabaseService(JobQueueRepository):
    """
    Backward compatibility shim for DatabaseService.
    Proxies pool access to the global db_core singleton.
    """
    @property
    def pool(self) -> Any:
        return db_core.pool
    
    @pool.setter
    def pool(self, value: Any) -> None:
        db_core.pool = value
