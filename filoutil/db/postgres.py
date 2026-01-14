import logging
import os
import time

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from filoutil.db.models import Base

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Default to a local postgres if not specified, or raise error in production
    DATABASE_URL = "postgresql://postgres:postgres@localhost:5001/filoutil"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    retries = 5
    while retries > 0:
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("Database initialized successfully.")
            return
        except OperationalError as e:
            retries -= 1
            logger.warning(f"Database not ready, retrying in 5 seconds... ({retries} retries left)")
            if retries == 0:
                logger.error("Could not connect to the database after multiple retries.")
                raise e
            time.sleep(5)
