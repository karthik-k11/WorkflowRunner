
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Task:
    task_id: str
    name: str
    task_type: str
    config: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    timeout_seconds: int = 30
    enabled: bool = True