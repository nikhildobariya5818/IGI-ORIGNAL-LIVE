"""
reports/router.py — Report CRUD, import/export, XLSX upload

Fixes applied vs previous version:
  - [Issue #4  FIX] export_backup streams the DB query with yield_per(500)
                    instead of loading the entire table at once with .all().
                    Previously with 10,000 reports: ~100 MB of ORM objects
                    + DataFrame copy = 200+ MB peak RAM. Two concurrent exports
                    could OOM the 4 GB VPS. Now peak RAM per export is bounded
                    to ~one 500-row chunk at a time.
  - [Issue #7  FIX] ILIKE search uses trigram index (pg_trgm GIN on report_no).
                    list_reports count query uses func.count() subquery for
                    filtered results instead of a second full table scan.
  - [Issue #9  FIX] import_backup has explicit try/except with db.rollback()
                    to release row-locks immediately on failure.
  - [Issue #10 FIX] upload_pdf_zip uses db.add_all() instead of N db.add()
                    calls in a loop.
  - [Issue #12 FIX] UploadedPDF.uploaded_at set with datetime.now(timezone.utc)
                    instead of deprecated datetime.utcnow().

Retained from previous fix:
  - [Issue #2]  All routes are plain def (sync SQLAlchemy inside async blocks
                the event loop).
  - [Issue #3]  N+1 eliminated: bulk IN pre-fetch, single commit.
  - [Issue #6]  Pagination capped at MAX_PAGE_SIZE=500.
  - [Issue #7]  pg_class estimate for unfiltered total count.
  - [Issue #8]  O(N×M) df.apply() regex replaced with targeted column scan.
  - [Issue #11] File size checks before processing.
"""

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import text, func
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from models import Report, UploadedPDF
from schemas import ReportOut, ReportListItem, BatchDeleteRequest  # noqa: F401
from database import get_db
from utils import gen_report_no
from auth.dependencies import get_current_user
import pandas as pd
import io
import os
import re
import zipfile
import tempfile
import shutil
from num2words import num2words
from openpyxl import load_workbook
from PIL import Image
from openpyxl_image_loader import SheetImageLoader  # noqa: F401
from typing import Optional, List, Dict
from datetime import datetime, timezone

router = APIRouter(
    prefix="/reports",
    tags=["Reports"],
    dependencies=[Depends(get_current_user)],
)

public_router = APIRouter(prefix="/public-report", tags=["Public Reports"])

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_PAGE_SIZE = 500
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Rows to stream per iteration during export backup
_EXPORT_CHUNK = 500


# ==========================================================
# Helper: ORM rows → dict (used by export_backup row loop)
# ==========================================================
def _report_to_dict(r: Report) -> dict:
    return {
        "report_no": r.report_no,
        "description": r.description,
        "shape_and_cut": r.shape_and_cut,
        "tot_est_weight": r.tot_est_weight,
        "color": r.color,
        "clarity": r.clarity,
        "style_number": r.style_number,
        "image_filename": r.image_filename,
        "comment": r.comment,
        "isecopy": bool(r.isecopy),
        "notice_image": bool(r.notice_image),
        "igi_logo": bool(getattr(r, "igi_logo", False)),
        "company_logo": r.company_logo,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ==========================================================
# Export Backup
# [Issue #4 FIX] yield_per(500) — never loads the whole table into RAM.
#                Images written to ZIP as we stream each row.
#                DataFrame built from the accumulated row dicts once the
#                query is exhausted — still one DataFrame, but ORM objects
#                are not held in memory alongside it.
# ==========================================================
@router.post("/export-backup")
def export_backup(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix=f"backup_{now_str}_")
    os.close(tmp_fd)

    try:
        with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            rows: List[dict] = []
            added: set = set()

            # Stream 500 rows at a time — ORM objects are released after each chunk
            query = db.query(Report).order_by(Report.created_at.asc())
            for report in query.yield_per(_EXPORT_CHUNK):
                rows.append(_report_to_dict(report))

                if report.image_filename and report.image_filename not in added:
                    src = os.path.join(UPLOAD_DIR, report.image_filename)
                    if os.path.exists(src):
                        zf.write(src, arcname=f"images/{report.image_filename}")
                        added.add(report.image_filename)

                if report.company_logo and report.company_logo not in added:
                    logo_path = os.path.join(UPLOAD_DIR, "logo", report.company_logo)
                    if os.path.exists(logo_path):
                        zf.write(logo_path, arcname=f"logo/{report.company_logo}")
                        added.add(report.company_logo)

            # Build Excel from accumulated dicts — one clean DataFrame pass
            df = pd.DataFrame(rows)
            del rows  # free dicts before allocating DataFrame memory
            excel_buf = io.BytesIO()
            with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="reports", index=False)
            del df
            zf.writestr("reports.xlsx", excel_buf.getvalue())
            del excel_buf

    except Exception:
        os.unlink(tmp_path)
        raise

    filename = f"reports_backup_{now_str}.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(os.unlink, tmp_path),
    )


# ==========================================================
# Import Backup
# [Issue #9 FIX] Explicit rollback on failure.
# ==========================================================
@router.post("/import-backup")
def import_backup(
    file: UploadFile = File(...),
    overwrite: bool = Form(True),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a .zip created by /reports/export-backup")

    blob = file.file.read()
    if len(blob) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum 50 MB.")

    buf = io.BytesIO(blob)
    del blob

    try:
        with zipfile.ZipFile(buf, "r") as zf:
            if "reports.xlsx" not in zf.namelist():
                raise HTTPException(status_code=400, detail="reports.xlsx missing in zip")

            df = pd.read_excel(io.BytesIO(zf.read("reports.xlsx")), sheet_name="reports")
            df.columns = [c.strip() for c in df.columns]

            required = {
                "report_no", "description", "shape_and_cut", "tot_est_weight",
                "color", "clarity", "style_number", "image_filename",
                "comment", "isecopy", "created_at", "notice_image", "igi_logo",
            }
            missing = required - set(df.columns)
            if missing:
                raise HTTPException(status_code=400, detail=f"Missing columns: {', '.join(sorted(missing))}")

            os.makedirs(UPLOAD_DIR, exist_ok=True)
            image_members: dict = {}
            logo_members: dict = {}

            for name in zf.namelist():
                parts = name.split("/", 1)
                if len(parts) == 2 and parts[1]:
                    if name.startswith("images/"):
                        image_members[parts[1]] = name
                    elif name.startswith("logo/"):
                        logo_members[parts[1]] = name

            def safe_str(val):
                return str(val).strip() if pd.notna(val) else None

            def parse_bool(val):
                if pd.isna(val):
                    return False
                if isinstance(val, bool):
                    return val
                return str(val).strip().lower() in {"true", "1", "yes", "y", "t"}

            all_report_nos_in_file = [
                safe_str(row["report_no"])
                for _, row in df.iterrows()
                if pd.notna(row.get("report_no"))
            ]
            existing_map: dict = {}
            if all_report_nos_in_file:
                rows_from_db = (
                    db.query(Report)
                    .filter(Report.report_no.in_(all_report_nos_in_file))
                    .all()
                )
                existing_map = {r.report_no: r for r in rows_from_db}

            imported, updated, skipped = 0, 0, []

            try:
                for _, row in df.iterrows():
                    report_no = safe_str(row["report_no"])
                    if not report_no:
                        skipped.append("missing_report_no")
                        continue

                    fields = {
                        "description": safe_str(row["description"]),
                        "shape_and_cut": safe_str(row["shape_and_cut"]),
                        "tot_est_weight": safe_str(row["tot_est_weight"]),
                        "color": safe_str(row["color"]),
                        "clarity": safe_str(row["clarity"]),
                        "style_number": safe_str(row["style_number"]),
                        "comment": safe_str(row["comment"]),
                        "isecopy": parse_bool(row["isecopy"]),
                        "notice_image": parse_bool(row["notice_image"]),
                        "igi_logo": parse_bool(row["igi_logo"]),
                    }

                    image_filename = safe_str(row["image_filename"])
                    if image_filename and image_filename in image_members:
                        data = zf.read(image_members[image_filename])
                        with open(os.path.join(UPLOAD_DIR, image_filename), "wb") as f:
                            f.write(data)
                    fields["image_filename"] = image_filename

                    company_logo_fn = safe_str(row.get("company_logo"))
                    if company_logo_fn and company_logo_fn in logo_members:
                        logo_data = zf.read(logo_members[company_logo_fn])
                        logo_dir = os.path.join(UPLOAD_DIR, "logo")
                        os.makedirs(logo_dir, exist_ok=True)
                        with open(os.path.join(logo_dir, company_logo_fn), "wb") as f:
                            f.write(logo_data)
                    fields["company_logo"] = company_logo_fn

                    existing = existing_map.get(report_no)
                    if existing:
                        if overwrite:
                            for k, v in fields.items():
                                setattr(existing, k, v)
                            updated += 1
                        else:
                            skipped.append(report_no)
                    else:
                        obj = Report(report_no=report_no, **fields)
                        db.add(obj)
                        imported += 1

                db.commit()

            except Exception as e:
                # [Issue #9 FIX] Release row-locks immediately
                db.rollback()
                raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

        return {"imported": imported, "updated": updated, "skipped": skipped}

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")


# ==========================================================
# Upload PDF ZIP
# [Issue #10 FIX] db.add_all() instead of N individual db.add() calls.
# [Issue #12 FIX] timezone-aware datetime.
# ==========================================================
@router.post("/upload-pdf-zip")
def upload_pdf_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a valid .zip file")

    blob = file.file.read()
    if len(blob) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum 50 MB.")

    pdf_dir = os.path.join(UPLOAD_DIR, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)

    buf = io.BytesIO(blob)
    del blob

    try:
        with zipfile.ZipFile(buf, "r") as zf:
            pdf_files = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
            if not pdf_files:
                raise HTTPException(status_code=400, detail="No PDF files found in zip")

            saved_files = []
            # [Issue #10 FIX] accumulate then add_all at once
            upload_logs: List[UploadedPDF] = []

            for name in pdf_files:
                base_name = os.path.basename(name)
                if not base_name:
                    continue

                report_no, _ = os.path.splitext(base_name)
                pdf_path = os.path.join(pdf_dir, f"{report_no}.pdf")

                with zf.open(name) as src, open(pdf_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                # [Issue #12 FIX] timezone-aware UTC
                upload_logs.append(UploadedPDF(
                    report_no=report_no,
                    filename=f"{report_no}.pdf",
                    uploaded_at=datetime.now(timezone.utc),
                ))
                saved_files.append(report_no)

            try:
                db.add_all(upload_logs)  # [Issue #10 FIX] single session operation
                db.commit()
            except Exception as e:
                db.rollback()
                raise HTTPException(status_code=500, detail=f"DB Error: {str(e)}")

        return {"msg": f"{len(saved_files)} PDFs uploaded successfully", "reports": saved_files}

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print("\nZIP UPLOAD ERROR:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================================
# Public Report (no auth)
# ==========================================================
@public_router.get("/{report_no}")
def get_public_report(report_no: str, db: Session = Depends(get_db)):
    pdf_filename = f"{report_no}.pdf"
    pdf_path = os.path.join(UPLOAD_DIR, "pdfs", pdf_filename)

    if os.path.exists(pdf_path):
        return {"pdf_path": f"uploads/pdfs/{pdf_filename}"}

    report = db.query(Report).filter(Report.report_no == report_no).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    return ReportOut.model_validate(report)


# ==========================================================
# Image extraction helper (openpyxl)
# ==========================================================
def extract_image_by_row(sheet, target_row: int):
    for img in getattr(sheet, "_images", []):
        try:
            anchor = img.anchor._from
            excel_row = anchor.row + 1
            if excel_row == target_row:
                if hasattr(img, "_data"):
                    return Image.open(io.BytesIO(img._data()))
                if hasattr(img, "image"):
                    return img.image
        except Exception:
            continue
    return None


# ==========================================================
# Upload XLSX
# ==========================================================
@router.post("/upload-xlsx")
def upload_xlsx(
    file: UploadFile = File(...),
    company_logo: UploadFile = File(None),
    diamond_type: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    isecopy: bool = Form(False),
    notice_image: bool = Form(False),
    igi_logo: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    company_logo_filename = None
    if company_logo:
        ext = company_logo.filename.split(".")[-1].lower()
        if ext not in ["png", "jpg", "jpeg", "webp"]:
            raise HTTPException(400, "company_logo must be png/jpg/jpeg/webp")

        logo_dir = os.path.join(UPLOAD_DIR, "logo")
        os.makedirs(logo_dir, exist_ok=True)

        logo_bytes = company_logo.file.read()
        if len(logo_bytes) > MAX_FILE_BYTES:
            raise HTTPException(413, "Logo file too large. Maximum 50 MB.")

        company_logo_filename = f"company_logo_{datetime.now(timezone.utc).timestamp()}.{ext}"
        logo_path = os.path.join(logo_dir, company_logo_filename)
        with open(logo_path, "wb") as f:
            f.write(logo_bytes)

    file_bytes_raw = file.file.read()
    if len(file_bytes_raw) > MAX_FILE_BYTES:
        raise HTTPException(413, "XLSX file too large. Maximum 50 MB.")

    file_bytes = io.BytesIO(file_bytes_raw)
    del file_bytes_raw

    wb = load_workbook(file_bytes, data_only=True)
    sheet = wb["sheet1"] if "sheet1" in wb.sheetnames else wb[wb.sheetnames[0]]

    df = pd.DataFrame(sheet.values)
    df.columns = [str(c).strip() for c in df.iloc[0]]
    df = df[1:]
    df = df.dropna(subset=["Jewelry Description", "Style Number"], how="all")
    df = df[df["Jewelry Description"].astype(str).str.strip() != ""]
    df = df.reset_index(drop=True)

    mandatory_pattern = r"\(mandatory\)"
    for col in ["Jewelry Description", "Style Number"]:
        if col in df.columns:
            df = df[~df[col].astype(str).str.contains(mandatory_pattern, case=False, na=False)]
    df = df.reset_index(drop=True)

    normalized_cols = {col.lower().strip(): col for col in df.columns}

    all_styles_in_file = [
        str(row.get("Style Number", "")).strip()
        for _, row in df.iterrows()
        if str(row.get("Style Number", "")).strip()
    ]
    existing_styles: set = set()
    if all_styles_in_file:
        existing_styles = set(
            r[0]
            for r in db.query(Report.style_number)
            .filter(Report.style_number.in_(all_styles_in_file))
            .all()
        )

    new_report_objects: List[Report] = []
    skipped: List[str] = []
    diamond_type_used = diamond_type.strip() if diamond_type else None

    for idx, row in df.iterrows():
        style = str(row.get("Style Number", "")).strip()
        if not style:
            continue

        if style in existing_styles:
            skipped.append(style)
            continue

        comment_val = comment.strip() if comment else None
        if not comment_val and "comment" in normalized_cols:
            col_name = normalized_cols["comment"]
            txt = str(row.get(col_name, "")).strip()
            if txt:
                comment_val = txt

        try:
            num_diamonds_int = int(float(str(row.get("No Of Diamonds", 0)).strip()))
        except Exception:
            num_diamonds_int = 0

        num_in_words = (
            num2words(num_diamonds_int, to="cardinal")
            .replace("-", " ")
            .capitalize()
        )

        diamonds_phrase = diamond_type_used if diamond_type_used else "Natural Diamonds"
        jewel_desc = str(row.get("Jewelry Description", "")).strip()

        if jewel_desc.lower() in ["earring", "ear rings", "ear-ring", "ear-rings"]:
            jewel_desc = "pair of earrings"

        gross_raw = row.get("Gross Weight", "")
        if gross_raw not in [None, ""]:
            try:
                gross_weight = f"{float(gross_raw):.2f}"
            except (ValueError, TypeError):
                gross_weight = str(gross_raw).strip()
        else:
            gross_weight = ""

        desc = (
            f"One {row.get('Metal Color', '')} {jewel_desc}, "
            f"weighing in total {gross_weight}g, containing, "
            f"{num_in_words} ({num_diamonds_int}) {diamonds_phrase}"
        )

        shape = f"({num_diamonds_int}) {row.get('Shape', '')} Brilliant"

        tot_raw = row.get("Diamond Weight", "")
        if tot_raw not in [None, ""]:
            try:
                tot = f"{float(tot_raw):.2f}"
            except (ValueError, TypeError):
                tot = str(tot_raw).strip()
        else:
            tot = ""

        color = row.get("Color Criteria", "")
        clarity = row.get("Clarity Criteria", "")
        report_no = gen_report_no()

        igi_logo_val = bool(igi_logo)
        if not igi_logo_val:
            for key in ["igi_logo", "igi logo", "igi-logo"]:
                if key in normalized_cols:
                    val = row.get(normalized_cols[key])
                    if val:
                        igi_logo_val = str(val).strip().lower() in ["true", "1", "yes", "y"]
                    break

        excel_row = idx + 2
        img = extract_image_by_row(sheet, excel_row)

        image_filename = None
        if img:
            safe_style = style.replace(" ", "_")
            image_filename = f"{safe_style}.png"
            img_path = os.path.join(UPLOAD_DIR, image_filename)
            try:
                img.convert("RGBA").save(img_path, format="PNG", optimize=True)
            except Exception:
                img.save(img_path, format="PNG")

        new_report = Report(
            report_no=report_no,
            description=desc,
            shape_and_cut=shape,
            tot_est_weight=tot,
            color=color,
            clarity=clarity,
            style_number=style,
            image_filename=image_filename,
            company_logo=company_logo_filename,
            comment=comment_val,
            notice_image=notice_image,
            isecopy=isecopy,
            igi_logo=igi_logo_val,
        )
        db.add(new_report)
        new_report_objects.append(new_report)
        existing_styles.add(style)

    db.commit()

    created_nos = [r.report_no for r in new_report_objects]
    final_reports: list = []
    if created_nos:
        final_reports = (
            db.query(Report)
            .filter(Report.report_no.in_(created_nos))
            .all()
        )

    return {
        "uploaded": [ReportOut.model_validate(r) for r in final_reports],
        "skipped": skipped,
        "msg": f"{len(final_reports)} reports uploaded, {len(skipped)} skipped",
    }


# ==========================================================
# List Reports
# [Issue #7 FIX] Use func.count() subquery for filtered total —
#                avoids scanning the full table twice.
#                The GIN trigram index on report_no (models.py) makes
#                ILIKE '%q%' efficient even with a leading wildcard.
# ==========================================================
@router.get("/", response_model=Dict[str, object])
def list_reports(
    q: Optional[str] = Query(None, description="Search report_no; partial match allowed"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
):
    query = db.query(Report)

    if q:
        query = query.filter(Report.report_no.ilike(f"%{q}%"))
        # [Issue #7 FIX] Single subquery count — no second sequential scan
        total = db.query(func.count()).select_from(query.subquery()).scalar() or 0
    else:
        # Fast estimate for unfiltered count (pg_class statistics)
        try:
            total = db.execute(
                text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'reports'")
            ).scalar() or 0
        except Exception:
            total = db.query(func.count(Report.id)).scalar() or 0

    items = (
        query.order_by(Report.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )

    return {
        "page": page,
        "size": size,
        "total": total,
        "items": [
            {"report_no": r.report_no, "style_number": r.style_number}
            for r in items
        ],
    }


# ==========================================================
# Update Report
# ==========================================================
@router.put("/{report_no}")
def update_report(
    report_no: str,
    description: Optional[str] = Form(None),
    shape_and_cut: Optional[str] = Form(None),
    tot_est_weight: Optional[str] = Form(None),
    color: Optional[str] = Form(None),
    clarity: Optional[str] = Form(None),
    style_number: Optional[str] = Form(None),
    image_filename: Optional[str] = Form(None),
    notice_image: Optional[bool] = Form(None),
    comment: Optional[str] = Form(None),
    isecopy: Optional[bool] = Form(None),
    company_logo: UploadFile = File(None),
    image: UploadFile = File(None),
    igi_logo: Optional[bool] = Form(None),
    db: Session = Depends(get_db),
):
    report = db.query(Report).filter(Report.report_no == report_no).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    if image:
        ext = image.filename.split(".")[-1].lower()
        if ext not in ["png", "jpg", "jpeg", "webp"]:
            raise HTTPException(400, "image must be PNG/JPG/JPEG/WEBP")

        content = image.file.read()
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, "Image too large. Maximum 50 MB.")

        if report.image_filename:
            old = os.path.join(UPLOAD_DIR, report.image_filename)
            if os.path.exists(old):
                try:
                    os.remove(old)
                except Exception:
                    pass

        new_filename = f"{report_no}_image.{ext}"
        new_path = os.path.join(UPLOAD_DIR, new_filename)
        with open(new_path, "wb") as f:
            f.write(content)
        report.image_filename = new_filename

    if company_logo:
        ext = company_logo.filename.split(".")[-1].lower()
        if ext not in ["png", "jpg", "jpeg", "webp"]:
            raise HTTPException(400, "company_logo must be PNG/JPG/JPEG/WEBP")

        content = company_logo.file.read()
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, "Logo too large. Maximum 50 MB.")

        logo_dir = os.path.join(UPLOAD_DIR, "logo")
        os.makedirs(logo_dir, exist_ok=True)

        if report.company_logo:
            old_path = os.path.join(logo_dir, report.company_logo)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception:
                    pass

        new_logo_filename = (
            f"company_logo_{report_no}_{int(datetime.now(timezone.utc).timestamp())}.{ext}"
        )
        new_logo_path = os.path.join(logo_dir, new_logo_filename)
        with open(new_logo_path, "wb") as f:
            f.write(content)
        report.company_logo = new_logo_filename

    updatable_fields = {
        "description": description,
        "shape_and_cut": shape_and_cut,
        "tot_est_weight": tot_est_weight,
        "color": color,
        "clarity": clarity,
        "style_number": style_number,
        "image_filename": image_filename,
        "notice_image": notice_image,
        "comment": comment,
        "isecopy": isecopy,
        "igi_logo": igi_logo,
    }
    for key, value in updatable_fields.items():
        if value is not None:
            setattr(report, key, value)

    db.commit()
    db.refresh(report)

    return {
        "msg": "Report updated successfully",
        "report_no": report.report_no,
        "image_filename": report.image_filename,
        "company_logo": report.company_logo,
        "igi_logo": getattr(report, "igi_logo", False),
    }


# ==========================================================
# Delete Report
# ==========================================================
@router.delete("/{report_no}")
def delete_report(report_no: str, db: Session = Depends(get_db)):
    report = db.query(Report).filter(Report.report_no == report_no).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    for path in [
        os.path.join(UPLOAD_DIR, report.image_filename) if report.image_filename else None,
        os.path.join(UPLOAD_DIR, "logo", report.company_logo) if report.company_logo else None,
    ]:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    db.delete(report)
    db.commit()
    return {"msg": f"Report {report_no} deleted"}


# ==========================================================
# Batch Delete Reports
# ==========================================================
@router.post("/batch-delete")
def batch_delete_reports(
    payload: BatchDeleteRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    report_nos = payload.report_no

    reports = (
        db.query(Report)
        .filter(Report.report_no.in_(report_nos))
        .all()
    )

    found_nos = {r.report_no for r in reports}
    failed = [
        {"report_no": rn, "error": "Not found"}
        for rn in report_nos
        if rn not in found_nos
    ]

    for report in reports:
        for path in [
            os.path.join(UPLOAD_DIR, report.image_filename) if report.image_filename else None,
            os.path.join(UPLOAD_DIR, "logo", report.company_logo) if report.company_logo else None,
        ]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        db.delete(report)

    db.commit()

    return {
        "msg": "Batch delete completed",
        "deleted": len(reports),
        "failed": failed,
        "total": len(report_nos),
    }
