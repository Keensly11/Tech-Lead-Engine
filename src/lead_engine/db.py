from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lead_engine.models import Base
from lead_engine.settings import DATABASE_URL

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
