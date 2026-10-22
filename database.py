# database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from pymongo import MongoClient

POSTGRES_URL = "postgresql://dev_user:dev_password@localhost:5432/main_db"
engine = create_engine(POSTGRES_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_pg_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

MONGO_URL = "mongodb://localhost:27017/"
mongo_client = MongoClient(MONGO_URL)
mongo_db = mongo_client["main_db"] 

def get_mongo_db():
    return mongo_db