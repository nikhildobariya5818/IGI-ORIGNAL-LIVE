"""
utils.py — shared utilities

Fixes applied vs previous version:
  - [passlib FIX] Replaced passlib.CryptContext with direct bcrypt calls.
                  passlib is unmaintained (last release 2020) and incompatible
                  with bcrypt 4.x+ on Python 3.13.
                  Two failure modes:
                    1. bcrypt.__about__.__version__ no longer exists → AttributeError
                    2. passlib's wrap-bug detector passes a 73-byte test password
                       directly to bcrypt, which now strictly rejects it → ValueError
                  Direct bcrypt calls have zero compatibility issues and are simpler.
"""

import bcrypt as _bcrypt
import jwt
from datetime import datetime, timedelta, timezone
import uuid
import os
import shutil
import time
from dotenv import load_dotenv
import random

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# bcrypt cost factor = 10
# Default is 12 (~300 ms on slow CPUs). 10 ≈ 75 ms — still OWASP-compliant.
_BCRYPT_ROUNDS = 10


def hash_password(password: str) -> str:
    # bcrypt hard-truncates at 72 bytes — pre-truncate explicitly so long
    # passwords don't silently compare equal after the 72nd byte.
    truncated = password[:72].encode("utf-8")
    return _bcrypt.hashpw(truncated, _bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    truncated = plain_password[:72].encode("utf-8")
    return _bcrypt.checkpw(truncated, hashed_password.encode("utf-8"))


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)


def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


import random
from datetime import datetime

def gen_report_no() -> str:
    # 2-digit prefix
    prefix = str(random.randint(10, 99))

    # Fixed middle letter
    middle = "J"

    # Generate 5 digits containing exactly one '1'
    pos = random.randint(0, 4)
    digits = [random.choice("023456789") for _ in range(5)]
    digits[pos] = "1"
    random_part = "".join(digits)

    # Current year and month
    now = datetime.now()
    yy = now.strftime("%y")
    mm = now.strftime("%m")

    return f"{prefix}{middle}{random_part}{mm}{yy}"



def cleanup_old_job_dirs(base_dir: str, max_age_seconds: int = 3600) -> int:
    if not os.path.isdir(base_dir):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(base_dir):
        path = os.path.join(base_dir, name)
        if not os.path.isdir(path):
            continue
        try:
            if (now - os.path.getmtime(path)) > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed