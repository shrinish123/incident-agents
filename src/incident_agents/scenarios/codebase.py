"""Synthetic source code for the two services in the scenario world.

The code is written for the dataset. It is modelled on the shape of a small
FastAPI + Celery product (API, worker, Postgres, Redis broker, object storage,
three LLM/embedding providers, a voice provider) and is not copied from any
real repository. It only has to be plausible to read, diff and search: it is
never executed.

`API_FILES` / `WORKER_FILES` hold the files at the head of the base history.
`API_HISTORY` / `WORKER_HISTORY` describe the commits that lead there; a
`patch` entry `(path, before, after)` means the file held `before` until that
commit.
"""

from __future__ import annotations

MAYA = "Maya Rao <maya@fivo.example>"
DEV = "Dev Menon <dev@fivo.example>"
ISHAAN = "Ishaan Kapoor <ishaan@fivo.example>"

# =============================================================================
# fivo-api
# =============================================================================

API_FILES: dict[str, str] = {}

API_FILES["README.md"] = """# fivo-api

HTTP API for Fivo, a personal finance tracker.

- FastAPI on a single 512 MB instance (sleeps when idle; a pinger hits `/health` every minute)
- Postgres (Neon) through SQLAlchemy
- Background work is handed to `fivo-worker` over a Redis (Upstash) queue
- Receipts are stored in R2; insights use Anthropic; reminder calls go through Bolna

## Run locally

    uv sync
    uv run uvicorn app.main:app --reload

Settings come from environment variables, see `app/config.py`.
"""

API_FILES["pyproject.toml"] = """[project]
name = "fivo-api"
version = "0.9.2"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.2",
    "alembic>=1.13",
    "celery[redis]>=5.4",
    "boto3>=1.35",
    "anthropic>=0.40",
    "httpx>=0.27",
]
"""

API_FILES["render.yaml"] = """services:
  - type: web
    name: fivo-api
    runtime: python
    plan: free
    buildCommand: uv sync --frozen
    startCommand: alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: REDIS_URL
        sync: false
      - key: DB_POOL_SIZE
        value: 10
      - key: RATE_LIMIT_RPM
        value: 240
"""

API_FILES["app/__init__.py"] = ""

API_FILES["app/config.py"] = '''"""Runtime settings, read once from the environment at import time."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "postgresql+psycopg://localhost/fivo")
    db_pool_size: int = _int("DB_POOL_SIZE", 10)
    db_pool_timeout_s: float = _float("DB_POOL_TIMEOUT_S", 5.0)

    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    quick_add_wait_ms: int = _int("QUICK_ADD_WAIT_MS", 8000)

    r2_endpoint: str = os.environ.get("R2_ENDPOINT", "")
    r2_bucket: str = os.environ.get("R2_BUCKET", "fivo-receipts")
    r2_access_key_id: str = os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.environ.get("R2_SECRET_ACCESS_KEY", "")

    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    anthropic_timeout_s: float = _float("ANTHROPIC_TIMEOUT_S", 20.0)

    bolna_api_key: str = os.environ.get("BOLNA_API_KEY", "")
    bolna_base_url: str = os.environ.get("BOLNA_BASE_URL", "https://api.bolna.example")
    bolna_timeout_s: float = _float("BOLNA_TIMEOUT_S", 10.0)

    rate_limit_rpm: int = _int("RATE_LIMIT_RPM", 240)


settings = Settings()
'''

API_FILES["app/db.py"] = '''"""Database engine and session factory."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=0,
    pool_timeout=settings.db_pool_timeout_s,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
'''

API_FILES["app/deps.py"] = '''"""Request dependencies."""

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .db import get_session
from .models import User


def current_user(authorization: str = Header(...), session: Session = Depends(get_session)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    user = session.query(User).filter(User.api_token == token).one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user
'''

API_FILES["app/models.py"] = '''"""ORM models."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    plan: Mapped[str] = mapped_column(String(20), default="free")
    device_count: Mapped[int] = mapped_column(Integer, default=1)
    api_token: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_transactions_idempotency_key"),
        Index("ix_transactions_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[str] = mapped_column(String(40))
    client_seq: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    merchant: Mapped[str] = mapped_column(String(120))
    merchant_tz: Mapped[str | None] = mapped_column(String(40), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    idempotency_key: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(60))
    size_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceCall(Base):
    __tablename__ = "voice_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    provider_call_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
'''

API_FILES["app/schemas.py"] = '''"""Request and response bodies."""

from pydantic import BaseModel, Field


class TransactionIn(BaseModel):
    device_id: str = Field(max_length=40)
    client_seq: int = Field(ge=1, description="Per-device entry counter. Each device numbers its own entries.")
    amount_minor: int = Field(gt=0)
    currency: str = "INR"
    merchant: str = Field(max_length=120)
    merchant_tz: str | None = None
    source: str = "manual"


class TransactionOut(BaseModel):
    id: int
    amount_minor: int
    currency: str
    merchant: str
    category: str | None = None


class ReceiptOut(BaseModel):
    id: int
    status: str


class VoiceCallIn(BaseModel):
    phone: str
    reminder: str = Field(max_length=280)
'''

API_FILES["app/logging_setup.py"] = '''"""Structured access logging."""

import json
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("fivo.access")


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        log.info(
            json.dumps(
                {
                    "method": request.method,
                    "route": request.scope.get("route").path if request.scope.get("route") else request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "trace_id": request.headers.get("x-trace-id", ""),
                }
            )
        )
        return response
'''

API_FILES["app/queue.py"] = '''"""Hand work to fivo-worker over the Redis broker."""

import time

from celery import Celery
from celery.result import AsyncResult
from kombu.exceptions import OperationalError

from .config import settings

celery = Celery("fivo", broker=settings.redis_url, backend=settings.redis_url)


class QueueUnavailable(RuntimeError):
    pass


def enqueue(task_name: str, *args) -> str:
    try:
        return celery.send_task(task_name, args=args).id
    except OperationalError as exc:
        raise QueueUnavailable(f"task queue unavailable: {exc}") from exc


def wait_for_result(job_id: str, timeout_ms: int):
    """Poll for a task result. Returns None when it is not ready in time."""
    deadline = time.monotonic() + timeout_ms / 1000
    result = AsyncResult(job_id, app=celery)
    while time.monotonic() < deadline:
        if result.ready():
            return result.get(propagate=False)
        time.sleep(0.15)
    return None
'''

API_FILES["app/main.py"] = '''"""Application entry point."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import settings
from .logging_setup import AccessLogMiddleware
from .middleware.ratelimit import RateLimitMiddleware
from .queue import QueueUnavailable
from .routes import categories, health, insights, receipts, summary, transactions, voice
from .services.errors import UpstreamError, UpstreamTimeout

app = FastAPI(title="fivo-api")
app.add_middleware(AccessLogMiddleware)
app.add_middleware(RateLimitMiddleware, rpm=settings.rate_limit_rpm)

for module in (health, transactions, receipts, summary, insights, voice, categories):
    app.include_router(module.router)


@app.exception_handler(QueueUnavailable)
async def queue_unavailable(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(UpstreamTimeout)
async def upstream_timeout(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=504)


@app.exception_handler(UpstreamError)
async def upstream_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=502)
'''

API_FILES["app/middleware/__init__.py"] = ""

API_FILES["app/middleware/ratelimit.py"] = '''"""Keeps the single small instance from being overloaded."""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed one-minute window over all requests to this instance."""

    def __init__(self, app, rpm: int) -> None:
        super().__init__(app)
        self._rpm = rpm
        self._window_started = time.monotonic()
        self._count = 0

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        now = time.monotonic()
        if now - self._window_started >= 60:
            self._window_started, self._count = now, 0
        self._count += 1
        if self._count > self._rpm:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "30"})
        return await call_next(request)
'''

API_FILES["app/routes/__init__.py"] = ""

API_FILES["app/routes/health.py"] = '''from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}
'''

API_FILES["app/routes/transactions.py"] = '''"""Create and list transactions."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..deps import current_user
from ..models import User
from ..queue import enqueue, wait_for_result
from ..repositories import transactions as repo
from ..schemas import TransactionIn
from ..services.idempotency import build_key

router = APIRouter()
CATEGORIZE = "categorize_transaction"


def _create(session: Session, user: User, body: TransactionIn):
    replay = repo.find_replay(session, user.id, body.device_id, body.client_seq)
    if replay is not None:
        return replay, False
    key = build_key(user.id, body.device_id, body.client_seq)
    return repo.insert(session, user.id, body, key), True


@router.post("/transactions", status_code=201)
def create_transaction(body: TransactionIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    txn, created = _create(session, user, body)
    if created:
        session.flush()
        enqueue(CATEGORIZE, txn.id)
    return repo.to_out(txn)


@router.post("/transactions/quick-add", status_code=201)
def quick_add(body: TransactionIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Create a transaction and wait briefly for its category, for the one-tap entry screen."""
    txn, created = _create(session, user, body)
    session.flush()
    session.commit()
    job_id = enqueue(CATEGORIZE, txn.id)
    category = wait_for_result(job_id, timeout_ms=settings.quick_add_wait_ms)
    if category is None:
        raise HTTPException(status_code=504, detail=f"categorisation result not ready after {settings.quick_add_wait_ms}ms")
    return repo.to_out(txn, category=category)


@router.get("/transactions")
def list_transactions(limit: int = 50, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return [repo.to_out(t) for t in repo.list_recent(session, user.id, min(limit, 200))]
'''

API_FILES["app/routes/receipts.py"] = '''"""Receipt upload and status."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import current_user
from ..models import Receipt, User
from ..queue import enqueue
from ..services import storage

router = APIRouter()
ALLOWED = {"image/jpeg", "image/png", "image/heic", "application/pdf"}


@router.post("/receipts", status_code=202)
def upload_receipt(file: UploadFile, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if file.content_type not in ALLOWED:
        raise HTTPException(status_code=415, detail="unsupported receipt type")
    blob = file.file.read()
    key = f"receipts/{user.id}/{uuid.uuid4().hex}"
    storage.put_object(key, blob, file.content_type)
    receipt = Receipt(user_id=user.id, object_key=key, content_type=file.content_type, size_bytes=len(blob))
    session.add(receipt)
    session.flush()
    enqueue("process_receipt", receipt.id)
    return {"id": receipt.id, "status": receipt.status}


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    receipt = session.get(Receipt, receipt_id)
    if receipt is None or receipt.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    return {"id": receipt.id, "status": receipt.status}
'''

API_FILES["app/routes/summary.py"] = '''from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import current_user
from ..models import User
from ..repositories import transactions as repo

router = APIRouter()


@router.get("/summary")
def summary(user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = repo.monthly_totals(session, user.id)
    return {"month_total_minor": sum(r.total_minor for r in rows), "by_category": [r._asdict() for r in rows]}
'''

API_FILES["app/routes/insights.py"] = '''"""Short written observations about this month's spending."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import current_user
from ..models import User
from ..repositories import transactions as repo
from ..services import anthropic_client

router = APIRouter()


@router.get("/insights")
def insights(user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = repo.monthly_totals(session, user.id)
    prompt = "Give three short, specific observations about this spending:\\n" + "\\n".join(
        f"{r.category}: {r.total_minor / 100:.2f}" for r in rows
    )
    text = anthropic_client.complete(prompt, max_tokens=400)
    return {"insights": text}
'''

API_FILES["app/routes/voice.py"] = '''"""Bill reminder calls through the voice provider."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import current_user
from ..models import User, VoiceCall
from ..schemas import VoiceCallIn
from ..services import bolna
from ..services.errors import UpstreamError

router = APIRouter()


@router.post("/voice/call", status_code=202)
def start_call(body: VoiceCallIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    call = VoiceCall(user_id=user.id, status="requested")
    session.add(call)
    session.flush()
    try:
        call.provider_call_id = bolna.start_call(body.phone, body.reminder)
        call.status = "queued"
    except UpstreamError as exc:
        call.status, call.error = "failed", str(exc)[:200]
        session.commit()
        raise
    return {"id": call.id, "status": call.status}
'''

API_FILES["app/routes/categories.py"] = '''from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Category

router = APIRouter()


@router.get("/categories")
def list_categories(session: Session = Depends(get_session)):
    return [{"id": c.id, "name": c.name} for c in session.query(Category).order_by(Category.name)]
'''

API_FILES["app/repositories/__init__.py"] = ""

API_FILES["app/repositories/transactions.py"] = '''"""Queries on the transactions table."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Category, Transaction
from ..schemas import TransactionIn, TransactionOut


def find_replay(session: Session, user_id: int, device_id: str, client_seq: int) -> Transaction | None:
    """The same device re-sending an entry it already delivered."""
    stmt = select(Transaction).where(
        Transaction.user_id == user_id, Transaction.device_id == device_id, Transaction.client_seq == client_seq
    )
    return session.execute(stmt).scalar_one_or_none()


def insert(session: Session, user_id: int, body: TransactionIn, key: str) -> Transaction:
    txn = Transaction(user_id=user_id, idempotency_key=key, **body.model_dump())
    session.add(txn)
    return txn


def list_recent(session: Session, user_id: int, limit: int = 50) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def monthly_totals(session: Session, user_id: int):
    month_start = func.date_trunc("month", func.now())
    stmt = (
        select(Category.name.label("category"), func.sum(Transaction.amount_minor).label("total_minor"))
        .join(Category, Category.id == Transaction.category_id, isouter=True)
        .where(Transaction.user_id == user_id, Transaction.created_at >= month_start)
        .group_by(Category.name)
    )
    return session.execute(stmt).all()


def to_out(txn: Transaction, category: str | None = None) -> TransactionOut:
    return TransactionOut(
        id=txn.id, amount_minor=txn.amount_minor, currency=txn.currency, merchant=txn.merchant, category=category
    )
'''

API_FILES["app/services/__init__.py"] = ""

API_FILES["app/services/errors.py"] = '''class UpstreamError(RuntimeError):
    """A third-party service answered with an error."""


class UpstreamTimeout(UpstreamError):
    """A third-party service did not answer in time."""
'''

API_FILES["app/services/idempotency.py"] = '''"""Idempotency keys for transaction creation.

Mobile clients queue entries while offline and may deliver the same entry more
than once. The key identifies one entry from one device.
"""


def build_key(user_id: int, device_id: str, client_seq: int) -> str:
    return f"{user_id}:{device_id}:{client_seq}"
'''

API_FILES["app/services/storage.py"] = '''"""Receipt storage on R2 (S3-compatible)."""

import boto3

from ..config import settings

_client = boto3.client(
    "s3",
    endpoint_url=settings.r2_endpoint,
    aws_access_key_id=settings.r2_access_key_id,
    aws_secret_access_key=settings.r2_secret_access_key,
    region_name="auto",
)


def put_object(key: str, blob: bytes, content_type: str) -> None:
    _client.put_object(Bucket=settings.r2_bucket, Key=key, Body=blob, ContentType=content_type)


def presigned_url(key: str, expires_s: int = 300) -> str:
    return _client.generate_presigned_url("get_object", Params={"Bucket": settings.r2_bucket, "Key": key}, ExpiresIn=expires_s)
'''

API_FILES["app/services/anthropic_client.py"] = '''"""Thin wrapper around the Anthropic SDK."""

import anthropic

from ..config import settings
from .errors import UpstreamError, UpstreamTimeout

_client = anthropic.Anthropic(
    api_key=settings.anthropic_api_key,
    timeout=settings.anthropic_timeout_s,
    max_retries=0,
)


def complete(prompt: str, max_tokens: int) -> str:
    try:
        message = _client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APITimeoutError as exc:
        raise UpstreamTimeout(f"anthropic request timed out after {settings.anthropic_timeout_s}s") from exc
    except anthropic.APIStatusError as exc:
        raise UpstreamError(f"anthropic returned {exc.status_code}") from exc
    return "".join(block.text for block in message.content if block.type == "text")
'''

API_FILES["app/services/bolna.py"] = '''"""Voice provider client."""

import httpx

from ..config import settings
from .errors import UpstreamError, UpstreamTimeout


def start_call(phone: str, reminder: str) -> str:
    try:
        response = httpx.post(
            f"{settings.bolna_base_url}/call",
            headers={"Authorization": f"Bearer {settings.bolna_api_key}"},
            json={"recipient_phone_number": phone, "user_data": {"reminder": reminder}},
            timeout=settings.bolna_timeout_s,
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout(f"bolna request timed out after {settings.bolna_timeout_s:g}s") from exc
    if response.status_code >= 500:
        raise UpstreamError(f"bolna upstream error: {response.status_code} {response.reason_phrase}")
    response.raise_for_status()
    return response.json()["execution_id"]
'''

API_FILES["migrations/env.py"] = '''from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
'''

API_FILES["migrations/versions/0001_initial.py"] = '''"""users, categories, transactions"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False, server_default="free"),
        sa.Column("api_token", sa.String(64), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table("categories", sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String(60), unique=True))
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount_minor", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("merchant", sa.String(120), nullable=False),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_transactions_user_created", "transactions", ["user_id", "created_at"])
'''

API_FILES["migrations/versions/0002_receipts.py"] = '''"""receipts"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.create_table(
        "receipts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("object_key", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(60), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploaded"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("embedding", sa.LargeBinary),
        sa.Column("embedded_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_receipts_user_id", "receipts", ["user_id"])
    op.create_index("ix_receipts_content_type", "receipts", ["content_type"])
'''

API_FILES["migrations/versions/0003_idempotency.py"] = '''"""offline sync: device id, per-device sequence, idempotency key"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.add_column("transactions", sa.Column("device_id", sa.String(40), nullable=False, server_default="legacy"))
    op.add_column("transactions", sa.Column("client_seq", sa.Integer, nullable=False, server_default="0"))
    op.add_column("transactions", sa.Column("source", sa.String(20), nullable=False, server_default="manual"))
    op.add_column("transactions", sa.Column("idempotency_key", sa.String(120)))
    op.execute("UPDATE transactions SET idempotency_key = user_id || ':legacy:' || id")
    op.alter_column("transactions", "idempotency_key", nullable=False)
    op.create_unique_constraint("uq_transactions_idempotency_key", "transactions", ["idempotency_key"])
    op.add_column("users", sa.Column("device_count", sa.Integer, nullable=False, server_default="1"))
'''

API_FILES["migrations/versions/0004_jobs.py"] = '''"""jobs: state of background work, written by fivo-worker"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("user_id", sa.Integer),
        sa.Column("ref_id", sa.BigInteger),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])
    op.create_index("ix_jobs_ref", "jobs", ["type", "ref_id"])
'''

API_FILES["migrations/versions/0005_voice_calls.py"] = '''"""voice_calls"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.create_table(
        "voice_calls",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_call_id", sa.String(60)),
        sa.Column("error", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_voice_calls_user_id", "voice_calls", ["user_id"])
'''

API_FILES["migrations/versions/0006_merchant_tz.py"] = '''"""transactions.merchant_tz, filled by card sync"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.add_column("transactions", sa.Column("merchant_tz", sa.String(40), nullable=True))
'''

API_FILES["tests/test_idempotency.py"] = '''from app.services.idempotency import build_key


def test_key_is_stable_for_a_redelivered_entry():
    assert build_key(7, "ios-1a2b", 12) == build_key(7, "ios-1a2b", 12)


def test_key_differs_between_entries():
    assert build_key(7, "ios-1a2b", 12) != build_key(7, "ios-1a2b", 13)
'''

API_FILES["tests/test_ratelimit.py"] = '''from app.middleware.ratelimit import RateLimitMiddleware


def test_window_counts_requests():
    mw = RateLimitMiddleware(app=None, rpm=2)
    assert mw._rpm == 2 and mw._count == 0
'''

API_HISTORY = [
    {
        "date": "2026-09-14T09:12:00Z", "author": MAYA, "message": "initial api: users, transactions, health",
        "add": [
            "README.md", "pyproject.toml", "render.yaml", "app/__init__.py", "app/config.py", "app/db.py", "app/deps.py",
            "app/models.py", "app/schemas.py", "app/main.py", "app/routes/__init__.py", "app/routes/health.py",
            "app/routes/transactions.py", "app/routes/summary.py", "app/routes/categories.py",
            "app/repositories/__init__.py", "app/repositories/transactions.py", "app/queue.py",
            "migrations/env.py", "migrations/versions/0001_initial.py",
        ],
    },
    {
        "date": "2026-09-22T15:40:00Z", "author": DEV, "message": "receipts: upload to R2 and hand off to the worker",
        "add": ["app/routes/receipts.py", "app/services/__init__.py", "app/services/storage.py", "migrations/versions/0002_receipts.py"],
    },
    {
        "date": "2026-09-30T11:05:00Z", "author": MAYA, "message": "offline sync: idempotency keys per device entry",
        "add": ["app/services/idempotency.py", "migrations/versions/0003_idempotency.py", "tests/test_idempotency.py"],
    },
    {
        "date": "2026-10-06T08:30:00Z", "author": ISHAAN, "message": "insights endpoint backed by Anthropic",
        "add": ["app/routes/insights.py", "app/services/anthropic_client.py", "app/services/errors.py", "migrations/versions/0004_jobs.py"],
    },
    {
        "date": "2026-10-15T13:20:00Z", "author": DEV, "message": "voice reminders through Bolna",
        "add": ["app/routes/voice.py", "app/services/bolna.py", "migrations/versions/0005_voice_calls.py"],
    },
    {
        "date": "2026-10-23T10:10:00Z", "author": MAYA, "message": "access log middleware and instance-wide rate limit",
        "add": ["app/logging_setup.py", "app/middleware/__init__.py", "app/middleware/ratelimit.py", "tests/test_ratelimit.py"],
        "patch": [("render.yaml", "        value: 10\n", "        value: 10\n      - key: RATE_LIMIT_RPM\n        value: 240\n")],
    },
    {
        "date": "2026-10-29T16:45:00Z", "author": ISHAAN, "message": "quick-add: wait up to 8s for the category",
        "patch": [("app/config.py", "    quick_add_wait_ms: int = _int(\"QUICK_ADD_WAIT_MS\", 5000)\n", "    quick_add_wait_ms: int = _int(\"QUICK_ADD_WAIT_MS\", 8000)\n")],
    },
    {
        "date": "2026-11-03T09:55:00Z", "author": DEV, "message": "store merchant timezone from card sync",
        "add": ["migrations/versions/0006_merchant_tz.py"],
        "patch": [("pyproject.toml", 'version = "0.9.1"', 'version = "0.9.2"')],
    },
]

# =============================================================================
# fivo-worker
# =============================================================================

WORKER_FILES: dict[str, str] = {}

WORKER_FILES["README.md"] = """# fivo-worker

Celery worker for Fivo. Consumes the Redis (Upstash) queue that `fivo-api` writes to.

| Task | Trigger | Calls |
|---|---|---|
| `categorize_transaction` | every new transaction | Groq |
| `process_receipt` | every receipt upload | R2, Anthropic, Voyage |
| `embed_pending_receipts` | every 10 minutes (beat) | Voyage |
| `daily_summary` | 03:00 UTC (beat) | Anthropic |

Runs as one 512 MB instance, one process with two worker threads.

    uv run celery -A worker.app worker --beat --pool=threads --loglevel=info
"""

WORKER_FILES["pyproject.toml"] = """[project]
name = "fivo-worker"
version = "0.6.4"
requires-python = ">=3.12"
dependencies = [
    "celery[redis]>=5.4",
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.2",
    "boto3>=1.35",
    "httpx>=0.27",
    "anthropic>=0.40",
    "pillow>=11.0",
    "pillow-heif>=0.20",
    "pypdf>=5.0",
]
"""

WORKER_FILES["render.yaml"] = """services:
  - type: worker
    name: fivo-worker
    runtime: python
    plan: starter
    buildCommand: uv sync --frozen
    startCommand: celery -A worker.app worker --beat --pool=threads --loglevel=info
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: REDIS_URL
        sync: false
      - key: WORKER_CONCURRENCY
        value: 2
"""

WORKER_FILES["worker/__init__.py"] = ""

WORKER_FILES["worker/config.py"] = '''"""Worker settings, read once from the environment at import time."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "postgresql+psycopg://localhost/fivo")
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    concurrency: int = int(os.environ.get("WORKER_CONCURRENCY", 2))

    r2_endpoint: str = os.environ.get("R2_ENDPOINT", "")
    r2_bucket: str = os.environ.get("R2_BUCKET", "fivo-receipts")
    r2_access_key_id: str = os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.environ.get("R2_SECRET_ACCESS_KEY", "")

    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    groq_model: str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    groq_timeout_s: float = float(os.environ.get("GROQ_TIMEOUT_S", 30))

    voyage_api_key: str = os.environ.get("VOYAGE_API_KEY", "")
    voyage_model: str = os.environ.get("VOYAGE_MODEL", "voyage-3-lite")

    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")


settings = Settings()
'''

WORKER_FILES["worker/app.py"] = '''"""Celery application and beat schedule."""

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure

from .config import settings

app = Celery("fivo", broker=settings.redis_url, backend=settings.redis_url, include=[
    "worker.tasks.categorize",
    "worker.tasks.receipts",
    "worker.tasks.embeddings",
    "worker.tasks.summary",
])

app.conf.update(
    worker_concurrency=settings.concurrency,
    worker_prefetch_multiplier=1,
    # A task is acknowledged only after it finishes, and is put back on the
    # queue if the process running it dies, so a deploy never drops work.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={"visibility_timeout": 600},
    result_expires=3600,
)

app.conf.beat_schedule = {
    "embed-pending-receipts": {"task": "embed_pending_receipts", "schedule": 600.0},
    "daily-summary": {"task": "daily_summary", "schedule": crontab(hour=3, minute=0)},
}


@task_failure.connect
def record_failure(task_id=None, exception=None, **_):
    """Any exception a task does not handle itself is written to the jobs table."""
    from . import repo

    repo.job_finished(task_id, "failed", repr(exception))
'''

WORKER_FILES["worker/db.py"] = '''from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings

engine = create_engine(settings.database_url, pool_size=2, max_overflow=1, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
'''

WORKER_FILES["worker/repo.py"] = '''"""Database access for tasks. Tables are owned by fivo-api migrations."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from .db import session_scope


@dataclass
class Txn:
    id: int
    user_id: int
    merchant: str
    merchant_tz: str | None
    amount_minor: int
    created_at: datetime


@dataclass
class ReceiptRow:
    id: int
    user_id: int
    object_key: str
    content_type: str


def get_transaction(transaction_id: int) -> Txn:
    with session_scope() as s:
        row = s.execute(
            text("SELECT id, user_id, merchant, merchant_tz, amount_minor, created_at FROM transactions WHERE id = :id"),
            {"id": transaction_id},
        ).one()
    return Txn(*row)


def set_category(transaction_id: int, label: str) -> None:
    with session_scope() as s:
        s.execute(
            text("UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = :label) WHERE id = :id"),
            {"label": label, "id": transaction_id},
        )


def get_receipt(receipt_id: int) -> ReceiptRow:
    with session_scope() as s:
        row = s.execute(
            text("SELECT id, user_id, object_key, content_type FROM receipts WHERE id = :id"), {"id": receipt_id}
        ).one()
    return ReceiptRow(*row)


def iter_pending_receipt_ids(limit: int = 64) -> list[int]:
    """Oldest receipts still missing an embedding. Capped so one run stays small."""
    with session_scope() as s:
        rows = s.execute(
            text("SELECT id FROM receipts WHERE status = 'processed' AND embedded_at IS NULL ORDER BY id LIMIT :n"), {"n": limit}
        )
        return [r[0] for r in rows]


def mark_receipt(receipt_id: int, status: str) -> None:
    with session_scope() as s:
        s.execute(
            text("UPDATE receipts SET status = :status, processed_at = now() WHERE id = :id"),
            {"status": status, "id": receipt_id},
        )


def save_embeddings(receipt_ids: list[int], vectors: list[list[float]]) -> None:
    with session_scope() as s:
        for rid, vec in zip(receipt_ids, vectors, strict=True):
            s.execute(text("UPDATE receipts SET embedding = :v, embedded_at = now() WHERE id = :id"), {"v": vec, "id": rid})


def job_started(job_id: str, kind: str, user_id: int | None, ref_id: int | None) -> None:
    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO jobs (id, type, user_id, ref_id, status, attempts, started_at) "
                "VALUES (:id, :type, :user_id, :ref_id, 'started', 1, now()) "
                "ON CONFLICT (id) DO UPDATE SET status = 'started', attempts = jobs.attempts + 1, started_at = now()"
            ),
            {"id": job_id, "type": kind, "user_id": user_id, "ref_id": ref_id},
        )


def job_finished(job_id: str, status: str, error: str | None = None) -> None:
    with session_scope() as s:
        s.execute(
            text("UPDATE jobs SET status = :status, last_error = :error, finished_at = now() WHERE id = :id"),
            {"status": status, "error": (error or "")[:300] or None, "id": job_id},
        )
'''

WORKER_FILES["worker/clients/__init__.py"] = ""

WORKER_FILES["worker/clients/errors.py"] = '''class UpstreamError(RuntimeError):
    """A provider answered with an error or not at all."""
'''

WORKER_FILES["worker/clients/groq.py"] = '''"""Transaction categorisation through Groq."""

import httpx

from ..config import settings
from .errors import UpstreamError

LABELS = ["groceries", "dining", "transport", "utilities", "rent", "shopping", "health", "entertainment", "other"]


def classify(merchant: str, features: dict) -> str:
    prompt = f"Merchant: {merchant}\\nSignals: {features}\\nAnswer with one of: {', '.join(LABELS)}"
    try:
        response = httpx.post(
            "https://api.groq.example/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={"model": settings.groq_model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8},
            timeout=settings.groq_timeout_s,
        )
    except httpx.TimeoutException as exc:
        raise UpstreamError(f"groq request timed out after {settings.groq_timeout_s:g}s") from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise UpstreamError(f"groq returned {response.status_code} {response.reason_phrase}")
    response.raise_for_status()
    label = response.json()["choices"][0]["message"]["content"].strip().lower()
    return label if label in LABELS else "other"
'''

WORKER_FILES["worker/clients/voyage.py"] = '''"""Embeddings through Voyage."""

import httpx

from ..config import settings
from .errors import UpstreamError


def embed(texts: list[str]) -> list[list[float]]:
    response = httpx.post(
        "https://api.voyage.example/v1/embeddings",
        headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
        json={"model": settings.voyage_model, "input": texts},
        timeout=60,
    )
    if response.status_code >= 500 or response.status_code == 429:
        raise UpstreamError(f"voyage returned {response.status_code}")
    response.raise_for_status()
    return [item["embedding"] for item in response.json()["data"]]
'''

WORKER_FILES["worker/clients/anthropic.py"] = '''"""Receipt field extraction and summaries through Anthropic."""

import base64

import anthropic

from ..config import settings
from .errors import UpstreamError

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60, max_retries=1)


def extract_receipt_fields(png: bytes) -> dict:
    try:
        message = _client.messages.create(
            model=settings.anthropic_model,
            max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
                {"type": "text", "text": "Return merchant, total and date from this receipt as JSON."},
            ]}],
        )
    except anthropic.APIError as exc:
        raise UpstreamError(f"anthropic failed: {type(exc).__name__}") from exc
    return {"raw": "".join(b.text for b in message.content if b.type == "text")}


def summarise(lines: list[str]) -> str:
    message = _client.messages.create(
        model=settings.anthropic_model, max_tokens=300,
        messages=[{"role": "user", "content": "Summarise yesterday's spending in two sentences:\\n" + "\\n".join(lines)}],
    )
    return "".join(b.text for b in message.content if b.type == "text")
'''

WORKER_FILES["worker/clients/storage.py"] = '''"""Receipt objects on R2 (S3-compatible)."""

import boto3

from ..config import settings

_client = boto3.client(
    "s3",
    endpoint_url=settings.r2_endpoint,
    aws_access_key_id=settings.r2_access_key_id,
    aws_secret_access_key=settings.r2_secret_access_key,
    region_name="auto",
)


def get_object(key: str) -> bytes:
    return _client.get_object(Bucket=settings.r2_bucket, Key=key)["Body"].read()


def put_object(key: str, blob: bytes, content_type: str) -> None:
    _client.put_object(Bucket=settings.r2_bucket, Key=key, Body=blob, ContentType=content_type)


def read_text(receipt_id: int) -> str:
    return get_object(f"extracted/{receipt_id}.txt").decode("utf-8", errors="replace")
'''

WORKER_FILES["worker/parsing.py"] = '''"""Turn an uploaded receipt into a PNG the extraction model can read."""

import io

import pillow_heif
from PIL import Image
from pypdf import PdfReader

MAX_SIDE = 1600


def decode_image(blob: bytes, content_type: str) -> bytes:
    if content_type == "application/pdf":
        page = PdfReader(io.BytesIO(blob)).pages[0]
        image = page.images[0].image
    elif content_type == "image/heic":
        heif = pillow_heif.read_heif(blob)  # native decoder (libheif)
        image = Image.frombytes(heif.mode, heif.size, heif.data, "raw")
    else:
        image = Image.open(io.BytesIO(blob))
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG")
    return out.getvalue()
'''

WORKER_FILES["worker/features.py"] = '''"""Signals passed to the categorisation model alongside the merchant name."""

from .repo import Txn


def amount_bucket(amount_minor: int) -> str:
    if amount_minor < 20_000:
        return "small"
    if amount_minor < 200_000:
        return "medium"
    return "large"


def time_features(txn: Txn) -> dict:
    hour = txn.created_at.hour
    return {"hour": hour, "is_weekend": txn.created_at.weekday() >= 5, "is_night": hour < 6 or hour >= 22}


def build(txn: Txn) -> dict:
    return {"amount": amount_bucket(txn.amount_minor), **time_features(txn)}
'''

WORKER_FILES["worker/tasks/__init__.py"] = ""

WORKER_FILES["worker/tasks/categorize.py"] = '''"""Assign a category to a new transaction."""

from .. import features, repo
from ..app import app
from ..clients import groq
from ..clients.errors import UpstreamError


@app.task(name="categorize_transaction", bind=True, max_retries=5)
def categorize_transaction(self, transaction_id: int) -> str:
    txn = repo.get_transaction(transaction_id)
    repo.job_started(self.request.id, "categorize_transaction", txn.user_id, transaction_id)
    signals = features.build(txn)
    try:
        label = groq.classify(txn.merchant, signals)
    except UpstreamError as exc:
        repo.job_finished(self.request.id, "retrying", str(exc))
        raise self.retry(exc=exc, countdown=2**self.request.retries)
    repo.set_category(transaction_id, label)
    repo.job_finished(self.request.id, "succeeded")
    return label
'''

WORKER_FILES["worker/tasks/receipts.py"] = '''"""Read an uploaded receipt: decode, extract fields, embed."""

from .. import parsing, repo
from ..app import app
from ..clients import anthropic, storage, voyage
from ..clients.errors import UpstreamError


@app.task(name="process_receipt", bind=True, max_retries=3)
def process_receipt(self, receipt_id: int) -> None:
    receipt = repo.get_receipt(receipt_id)
    repo.job_started(self.request.id, "process_receipt", receipt.user_id, receipt_id)
    blob = storage.get_object(receipt.object_key)
    png = parsing.decode_image(blob, receipt.content_type)
    try:
        fields = anthropic.extract_receipt_fields(png)
        vector = voyage.embed([fields["raw"]])[0]
    except UpstreamError as exc:
        repo.job_finished(self.request.id, "retrying", str(exc))
        raise self.retry(exc=exc, countdown=10)
    storage.put_object(f"extracted/{receipt_id}.txt", fields["raw"].encode(), "text/plain")
    repo.save_embeddings([receipt_id], [vector])
    repo.mark_receipt(receipt_id, "processed")
    repo.job_finished(self.request.id, "succeeded")
'''

WORKER_FILES["worker/tasks/embeddings.py"] = '''"""Backfill embeddings for receipts that were processed without one."""

from itertools import islice

from .. import repo
from ..app import app
from ..clients import storage, voyage

BATCH_SIZE = 16


def chunked(items, size):
    it = iter(items)
    while batch := list(islice(it, size)):
        yield batch


@app.task(name="embed_pending_receipts", bind=True)
def embed_pending_receipts(self) -> int:
    repo.job_started(self.request.id, "embed_pending_receipts", None, None)
    done = 0
    for batch in chunked(repo.iter_pending_receipt_ids(), BATCH_SIZE):
        texts = [storage.read_text(receipt_id) for receipt_id in batch]
        repo.save_embeddings(batch, voyage.embed(texts))
        done += len(batch)
    repo.job_finished(self.request.id, "succeeded")
    return done
'''

WORKER_FILES["worker/tasks/summary.py"] = '''"""Nightly spending summary per user."""

from sqlalchemy import text

from .. import repo
from ..app import app
from ..clients import anthropic
from ..db import session_scope


@app.task(name="daily_summary", bind=True)
def daily_summary(self) -> int:
    repo.job_started(self.request.id, "daily_summary", None, None)
    with session_scope() as s:
        rows = s.execute(
            text(
                "SELECT user_id, merchant, amount_minor FROM transactions "
                "WHERE created_at >= now() - interval '1 day' ORDER BY user_id"
            )
        ).all()
    by_user: dict[int, list[str]] = {}
    for user_id, merchant, amount in rows:
        by_user.setdefault(user_id, []).append(f"{merchant}: {amount / 100:.2f}")
    for lines in by_user.values():
        anthropic.summarise(lines)
    repo.job_finished(self.request.id, "succeeded")
    return len(by_user)
'''

WORKER_FILES["tests/test_features.py"] = '''from datetime import datetime, timezone

from worker.features import amount_bucket, time_features
from worker.repo import Txn


def test_amount_buckets():
    assert amount_bucket(5_000) == "small" and amount_bucket(50_000) == "medium" and amount_bucket(900_000) == "large"


def test_night_flag():
    txn = Txn(1, 1, "Chai Point", "Asia/Kolkata", 4_000, datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc))
    assert time_features(txn)["is_night"] is True
'''

WORKER_HISTORY = [
    {
        "date": "2026-09-16T10:20:00Z", "author": MAYA, "message": "initial worker: categorise transactions with Groq",
        "add": [
            "README.md", "pyproject.toml", "render.yaml", "worker/__init__.py", "worker/config.py", "worker/app.py",
            "worker/db.py", "worker/repo.py", "worker/clients/__init__.py", "worker/clients/errors.py",
            "worker/clients/groq.py", "worker/features.py", "worker/tasks/__init__.py", "worker/tasks/categorize.py",
            "tests/test_features.py",
        ],
    },
    {
        "date": "2026-09-24T14:05:00Z", "author": DEV, "message": "process receipts: decode, extract with Anthropic, embed with Voyage",
        "add": ["worker/parsing.py", "worker/clients/anthropic.py", "worker/clients/voyage.py", "worker/clients/storage.py", "worker/tasks/receipts.py"],
    },
    {
        "date": "2026-10-02T09:40:00Z", "author": DEV, "message": "beat: backfill receipt embeddings every 10 minutes",
        "add": ["worker/tasks/embeddings.py"],
    },
    {
        "date": "2026-10-09T12:15:00Z", "author": ISHAAN, "message": "nightly spending summary",
        "add": ["worker/tasks/summary.py"],
    },
    {
        "date": "2026-10-17T17:30:00Z", "author": MAYA, "message": "do not lose tasks on deploy: late acks, requeue when a process dies",
        "patch": [(
            "worker/app.py",
            "    worker_prefetch_multiplier=1,\n    broker_transport_options",
            "    worker_prefetch_multiplier=1,\n"
            "    # A task is acknowledged only after it finishes, and is put back on the\n"
            "    # queue if the process running it dies, so a deploy never drops work.\n"
            "    task_acks_late=True,\n    task_reject_on_worker_lost=True,\n    broker_transport_options",
        )],
    },
    {
        "date": "2026-10-26T08:50:00Z", "author": DEV, "message": "receipts: accept HEIC uploads from iOS",
        "patch": [
            ("pyproject.toml", '    "pillow>=11.0",\n    "pypdf>=5.0",', '    "pillow>=11.0",\n    "pillow-heif>=0.20",\n    "pypdf>=5.0",'),
            (
                "worker/parsing.py",
                "        image = page.images[0].image\n    else:",
                "        image = page.images[0].image\n    elif content_type == \"image/heic\":\n"
                "        heif = pillow_heif.read_heif(blob)  # native decoder (libheif)\n"
                "        image = Image.frombytes(heif.mode, heif.size, heif.data, \"raw\")\n    else:",
            ),
            ("worker/parsing.py", "import io\n\nfrom PIL import Image", "import io\n\nimport pillow_heif\nfrom PIL import Image"),
        ],
    },
    {
        "date": "2026-11-02T11:25:00Z", "author": ISHAAN, "message": "groq: retry with backoff, 30s request timeout",
        "patch": [
            ("worker/config.py", 'os.environ.get("GROQ_TIMEOUT_S", 10)', 'os.environ.get("GROQ_TIMEOUT_S", 30)'),
            ("pyproject.toml", 'version = "0.6.3"', 'version = "0.6.4"'),
        ],
    },
]

REPOS = {
    "fivo-api": (API_FILES, API_HISTORY),
    "fivo-worker": (WORKER_FILES, WORKER_HISTORY),
}
