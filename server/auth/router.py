"""
auth/router.py — Authentication endpoints

Fixes applied vs previous version:
  - [Issue #1  FIX] CRITICAL: Limiter imported from shared limiter.py instead
                    of being re-created locally.

                    The previous code did:
                        limiter = Limiter(key_func=get_remote_address)
                    This new local instance was never attached to app.state,
                    so slowapi's @limiter.limit() decorator found no state
                    storage and silently skipped all limits.

                    Result: /auth/login was completely unrate-limited.
                    At bcrypt rounds=10 (~75 ms each), 13 concurrent login
                    attempts = 975 ms of continuous CPU = 100% on KVM1 vCPU.
                    A brute-force script with 13 threads could peg the CPU
                    indefinitely. This fix makes the 5/minute limit real.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from limiter import limiter           # [Issue #1 FIX] shared, app-attached instance
from models import User
from database import get_db
from schemas import UserCreate, Token
from utils import create_access_token, verify_password, hash_password
from auth.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("3/minute")
def register_user(request: Request, user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    raw_password = user.password.get_secret_value()
    hashed_password = hash_password(raw_password)

    db_user = User(email=user.email, hashed_password=hashed_password)
    db.add(db_user)
    db.commit()

    return {"msg": "User registered successfully"}


@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
def login_user(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == form.username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.email})
    return {"access_token": token, "token_type": "bearer"}


@router.get("/verify-token")
def verify_token_endpoint(current_user: dict = Depends(get_current_user)):
    return {
        "email": current_user.get("sub"),
        "status": "Token is valid",
    }
