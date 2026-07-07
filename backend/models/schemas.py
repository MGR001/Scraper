from typing import Optional

from pydantic import BaseModel


class SourceCreate(BaseModel):
    name: str
    url: str
    category: str = "general"   # competitor | news | market | general
    scrape_interval: int = 24   # hours


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    scrape_interval: Optional[int] = None
    is_active: Optional[bool] = None


class ChatMessage(BaseModel):
    message: str
