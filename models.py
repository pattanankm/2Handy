# models.py
from sqlalchemy import Column, Integer, String, DateTime
from database import Base
import datetime


class User(Base):
    __tablename__ = "users"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    name = Column(
        String,
        index=True
    )

    email = Column(
        String,
        unique=True,
        index=True
    )

    created_at = Column(
        DateTime,
        default=datetime.datetime.utcnow
    )