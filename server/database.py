"""
database.py — SQLAlchemy engine + session factory

Fixes applied vs previous version:
  - [Issue #9  FIX] get_db() now calls db.rollback() on any exception before
                    closing. Without this, a failed transaction with autoflush=False
                    can leave PostgreSQL row-locks held until the connection is
                    recycled (up to pool_recycle=1800 seconds).
  - pg_trgm extension ensured at startup (required for the trigram index on
    Report.report_no that fixes the ILIKE full-table-scan — Issue #7).

Retained from previous fix:
  - pool_size=2, max_overflow=3 tuned for KVM1 (1 vCPU)
  - pool_timeout=10, pool_recycle=1800, pool_pre_ping=True
  - echo=False always
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


# -------------------------
# AUTO-CREATE DATABASE
# -------------------------
def _create_database_if_not_exists():
    try:
        url = make_url(DATABASE_URL.replace("+psycopg", ""))
        db_name = url.database
        admin_url = f"postgresql://{url.username}:{url.password}@{url.host}"

        admin_engine = create_engine(admin_url, future=True, pool_size=1, max_overflow=0)
        with admin_engine.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db_name}
            ).scalar()
            if not exists:
                print(f"Database '{db_name}' not found — creating...")
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                print("Database created.")
            else:
                print(f"Database '{db_name}' already exists.")
        admin_engine.dispose()
    except Exception as e:
        print(f"Database check/create failed: {e}")


_create_database_if_not_exists()

# -------------------------
# ENGINE — tuned for KVM1 (1 vCPU, limited RAM)
# -------------------------
engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_size=2,
    max_overflow=3,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)

try:
    with engine.connect() as conn:
        print(f"Connected to database '{conn.engine.url.database}'")

        # [Issue #7 DEPENDENCY] pg_trgm is required for the GIN trigram index
        # on Report.report_no (see models.py). Idempotent — safe to run on
        # every startup. Requires PostgreSQL superuser or pg_extension role.
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.commit()
        except Exception as ext_err:
            print(f"pg_trgm extension warning (needs superuser): {ext_err}")

except OperationalError as e:
    print(f"Database connection failed: {e}")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


# -------------------------
# [Issue #9 FIX] Session dependency with explicit rollback
# -------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()   # release row-locks immediately on any error
        raise
    finally:
        db.close()
