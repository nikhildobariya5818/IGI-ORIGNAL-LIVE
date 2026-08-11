"""
models.py — SQLAlchemy ORM models

Fixes applied vs previous version:
  - [Issue #7  FIX] GIN trigram index added on Report.report_no.
                    ILIKE '%query%' with a leading wildcard cannot use a
                    standard B-tree index — it forces a full sequential scan
                    on every search request. pg_trgm GIN indexes support
                    leading-wildcard ILIKE efficiently. Requires the pg_trgm
                    extension (enabled automatically in database.py startup).
  - [Issue #12 FIX] UploadedPDF.uploaded_at now uses timezone-aware UTC
                    (datetime.now(timezone.utc)) instead of the deprecated
                    datetime.utcnow which is removed in Python 3.13 and
                    returns a tz-naive datetime that does not match Report's
                    tz-aware created_at column.
"""

from sqlalchemy import Column, Integer, String, DateTime, func, Index, Boolean
from sqlalchemy.dialects.postgresql import TSVECTOR  # noqa: F401 — available if needed later
from database import Base
from datetime import datetime, timezone

try:
    import importlib
    citext = importlib.import_module("citext")
    CIText = getattr(citext, "CIText")
    EmailType = CIText
except (ImportError, ModuleNotFoundError, AttributeError):
    EmailType = String


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(
        EmailType if EmailType is not String else String(254),
        unique=True,
        index=True,
        nullable=False,
    )
    hashed_password = Column(String(128), nullable=False)


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    report_no = Column(String(32), unique=True, index=True, nullable=False)
    description = Column(String(4000), nullable=False)
    shape_and_cut = Column(String(255), nullable=False)
    tot_est_weight = Column(String(255), nullable=False)
    color = Column(String(64))
    clarity = Column(String(64))
    style_number = Column(String(255), index=True)
    image_filename = Column(String(512))
    comment = Column(String(1000))
    isecopy = Column(Boolean, default=False)
    company_logo = Column(String, nullable=True)
    notice_image = Column(Boolean, default=False)
    igi_logo = Column(Boolean, default=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Composite index for list/filter queries ordered by created_at
        Index("ix_reports_style_created", "style_number", "created_at"),
        # [Issue #7 FIX] GIN trigram index for ILIKE '%query%' on report_no.
        # Turns a full sequential scan into an index scan.
        # Requires: CREATE EXTENSION IF NOT EXISTS pg_trgm; (done in database.py)
        Index(
            "ix_reports_report_no_trgm",
            "report_no",
            postgresql_using="gin",
            postgresql_ops={"report_no": "gin_trgm_ops"},
        ),
    )


class UploadedPDF(Base):
    __tablename__ = "uploaded_pdfs"
    id = Column(Integer, primary_key=True, index=True)
    report_no = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    # [Issue #12 FIX] timezone=True + tz-aware default
    uploaded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
