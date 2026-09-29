from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from lead_engine.models import Base
from lead_engine.settings import DATABASE_URL

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# Columns added after the first release: (table, column, SQL type).
# create_all() only creates missing tables, so existing databases get these via ALTER TABLE.
_ADDED_COLUMNS = [
    ("company", "domain_source", "VARCHAR(20)"),
    ("company", "contact_checked_at", "TIMESTAMP"),
]


def init_db(bind=engine) -> None:
    Base.metadata.create_all(bind)
    existing = {t: {c["name"] for c in inspect(bind).get_columns(t)} for t in {t for t, _, _ in _ADDED_COLUMNS}}
    with bind.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            if column not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
