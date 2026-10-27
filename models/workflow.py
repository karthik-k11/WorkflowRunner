
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


@dataclass
class Workflow:
    name: str
    description: str = ""
    workflow_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)