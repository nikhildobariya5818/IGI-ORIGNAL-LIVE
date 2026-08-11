# gunicorn.conf.py — Production config for Hostinger KVM1 (1 vCPU, 4 GB RAM)
#
# Start with:
#   gunicorn -c gunicorn.conf.py main:app
#
# Changes vs previous version:
#   - [Issue #2 FIX] post_fork() hook added: disposes the SQLAlchemy connection
#                    pool in each child process immediately after fork().
#                    Without this, preload_app=True causes all workers to
#                    SHARE the master process's DB TCP sockets (same OS file
#                    descriptors). Two workers writing to the same PostgreSQL
#                    socket simultaneously produces corrupted protocol frames:
#                    InterfaceError: connection already closed
#                    OperationalError: SSL connection has been closed unexpectedly
#                    Workers crash → Gunicorn restarts → heavy library re-import
#                    (numpy, pandas, fitz) = CPU spike on every crash.
#
#   - [Issue #3 FIX] workers reduced from 3 → 2.
#                    threads reduced from 4 → 2.
#                    The (2×CPU)+1=3 formula is for I/O-bound apps.
#                    PDF processing (PyMuPDF + NumPy + barcode) is CPU-bound.
#                    3 workers doing simultaneous PDF processing on 1 vCPU
#                    means 300% CPU demand — all three run ~3× slower due to
#                    OS context-switching thrash.
#                    2 workers: one handles normal API traffic, one does PDF.
#                    2 threads each: 4 total sync handlers, less scheduler noise.

# -------------------------------------------------------
# Workers & threads
# -------------------------------------------------------
workers = 2               # [Issue #3 FIX] was 3; CPU-bound workload needs fewer
worker_class = "uvicorn.workers.UvicornWorker"
threads = 2               # [Issue #3 FIX] was 4; 2×2=4 total, less contention

# -------------------------------------------------------
# Timeouts
# PDF processing can take 20–60 s on a low-power VPS.
# -------------------------------------------------------
timeout = 120
graceful_timeout = 30

# -------------------------------------------------------
# Memory leak guard
# PIL, fitz, pandas all have minor known leaks in
# long-running processes. Recycle workers periodically.
# -------------------------------------------------------
max_requests = 500
max_requests_jitter = 50  # stagger restarts so not all workers recycle at once

# -------------------------------------------------------
# Binding
# -------------------------------------------------------
bind = "127.0.0.1:8000"

# -------------------------------------------------------
# Logging — warning level only in production.
# info/debug generates significant I/O under load.
# -------------------------------------------------------
accesslog = "-"
errorlog = "-"
loglevel = "warning"

# -------------------------------------------------------
# Preload app
# Load FastAPI app once in master, then fork workers from
# the preloaded state. Saves ~50 MB RAM per worker.
# REQUIRES post_fork() to dispose the DB connection pool
# (see below — Issue #2 fix).
# -------------------------------------------------------
preload_app = True

keepalive = 2


# -------------------------------------------------------
# [Issue #2 FIX] post_fork hook — CRITICAL
#
# After Gunicorn forks a worker from the preloaded master,
# the worker inherits all open file descriptors including
# SQLAlchemy's DB connection pool sockets.
#
# engine.dispose() discards those inherited sockets so the
# worker creates its own fresh connections on first use.
# Without this, workers share sockets → corrupted protocol
# frames → random InterfaceErrors → worker crashes → CPU
# spike from heavy library re-imports on every restart.
# -------------------------------------------------------
def post_fork(server, worker):
    from database import engine
    engine.dispose()
