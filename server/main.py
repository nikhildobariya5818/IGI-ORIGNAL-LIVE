"""
main.py — IGI FastAPI Backend (Production Ready)
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
import os

from limiter import limiter
from database import Base, engine
from auth.router import router as auth_router
from reports.router import router as reports_router, public_router
from models import *  # noqa
from dotenv import load_dotenv

load_dotenv()

# -------------------------
# CREATE TABLES
# -------------------------
Base.metadata.create_all(bind=engine)

# -------------------------
# APP
# -------------------------
app = FastAPI(title="IGI FastAPI Backend")

from fastapi.staticfiles import StaticFiles

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
# -------------------------
# RATE LIMITER
# -------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# -------------------------
# UPLOAD SIZE LIMIT
# -------------------------
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")

        if content_length:
            try:
                if int(content_length) > MAX_UPLOAD_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large. Maximum is 100 MB."},
                    )
            except ValueError:
                pass
        else:
            received = 0
            chunks = []

            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_UPLOAD_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large. Maximum is 100 MB."},
                    )
                chunks.append(chunk)

            body = b"".join(chunks)

            async def _receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = _receive  # type: ignore

    return await call_next(request)


# -------------------------
# DIRECTORIES (IMPORTANT FIX)
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OUTPUT_DIR = os.path.join(BASE_DIR, "files")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# -------------------------
# CORS
# -------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://igi.org.pe",
        "https://api.igi.org.pe",
        "https://www.igi.org.pe",
        "https://www.api.igi.org.pe",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# ROUTERS
# -------------------------
app.include_router(auth_router)
app.include_router(reports_router)
app.include_router(public_router)


# -------------------------
# HEALTH CHECK / ROOT
# -------------------------
@app.get("/")
def home():
    return {"message": "IGI FastAPI Backend running modify"}
