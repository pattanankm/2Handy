#from polly's branch
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from datetime import datetime


class Product(BaseModel):
    name: str
    category: str
    price: float
    attributes: Dict[str, Any] = Field(default_factory=dict)


class Review(BaseModel):
    product_id: str
    user_id: int
    rating: int
    comment: str
    created_at: Optional[datetime] = None