from pydantic import BaseModel, Field
from typing import Optional

class CreateTask(BaseModel):
    goal: str = Field(min_length=3, max_length=10000)

class RunResponse(BaseModel):
    task_id: str
    status: str
    message: str
