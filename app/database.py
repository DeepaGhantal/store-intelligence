"""
database.py — SQLAlchemy setup with SQLite (swappable to Postgres via DATABASE_URL env).
"""
import os
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class EventRecord(Base):
    __tablename__ = "events"

    event_id    = Column(String, primary_key=True, index=True)
    store_id    = Column(String, index=True)
    camera_id   = Column(String)
    visitor_id  = Column(String, index=True)
    event_type  = Column(String, index=True)
    timestamp   = Column(String, index=True)
    zone_id     = Column(String, nullable=True)
    dwell_ms    = Column(Integer, default=0)
    is_staff    = Column(Boolean, default=False)
    confidence  = Column(Float)
    queue_depth = Column(Integer, nullable=True)
    sku_zone    = Column(String, nullable=True)
    session_seq = Column(Integer, default=0)
    ingested_at = Column(String)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
