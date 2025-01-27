from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class Assignment:
    id: int
    name: str
    description: str
    due_date: datetime
    course_id: int
    course_name: str
    status: str = "Not started"
    grade: Optional[float] = None
    group_name: Optional[str] = None
    group_weight: Optional[float] = None
    priority: Optional[str] = "Low"