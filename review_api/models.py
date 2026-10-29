"""PostgreSQL business records. Schema changes are applied through Alembic."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ReviewTask(Base):
    __tablename__ = 'review_tasks'
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    idempotency_key: Mapped[UUID] = mapped_column(PGUUID, nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(39), nullable=False)
    repo: Mapped[str] = mapped_column(String(100), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default='1')
    execution_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default='queued')
    review_status: Mapped[str | None] = mapped_column(String(24))
    publication_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default='disabled')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default='0')
    resource_records_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('true'))
    summary: Mapped[str | None] = mapped_column(Text)
    total_files: Mapped[int | None] = mapped_column(Integer)
    reviewed_files: Mapped[int | None] = mapped_column(Integer)
    failed_files: Mapped[int | None] = mapped_column(Integer)
    skipped_files: Mapped[int | None] = mapped_column(Integer)
    excluded_files: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("execution_status IN ('queued','running','finished','failed','interrupted')", name='ck_task_execution'),
        CheckConstraint("publication_state = 'disabled'", name='ck_task_publication'),
        CheckConstraint("schema_version = 1 AND pr_number > 0 AND last_event_seq >= 0", name='ck_task_numbers'),
        CheckConstraint("head_sha ~ '^[0-9a-f]{40}$' AND base_sha ~ '^[0-9a-f]{40}$' AND request_fingerprint ~ '^[0-9a-f]{64}$'", name='ck_task_hashes'),
        CheckConstraint("jsonb_typeof(config) = 'object'", name='ck_task_config'),
        CheckConstraint("(execution_status = 'finished' AND review_status IS NOT NULL AND review_status IN ('COMPLETE','INCOMPLETE','NO_ELIGIBLE_FILES') AND summary IS NOT NULL AND error_code IS NULL) OR (execution_status <> 'finished' AND review_status IS NULL AND summary IS NULL)", name='ck_task_review_state'),
        CheckConstraint("(execution_status IN ('queued','running') AND finished_at IS NULL AND error_code IS NULL) OR (execution_status IN ('finished','failed','interrupted') AND finished_at IS NOT NULL)", name='ck_task_terminal_time'),
        CheckConstraint("(execution_status = 'queued' AND started_at IS NULL) OR (execution_status IN ('running','finished') AND started_at IS NOT NULL) OR execution_status IN ('failed','interrupted')", name='ck_task_start_time'),
        CheckConstraint("execution_status NOT IN ('failed','interrupted') OR error_code IS NOT NULL", name='ck_task_error'),
        CheckConstraint("(execution_status = 'finished' AND total_files IS NOT NULL AND reviewed_files IS NOT NULL AND failed_files IS NOT NULL AND skipped_files IS NOT NULL AND excluded_files IS NOT NULL AND total_files = reviewed_files + failed_files + skipped_files + excluded_files AND reviewed_files >= 0 AND failed_files >= 0 AND skipped_files >= 0 AND excluded_files >= 0) OR (execution_status <> 'finished' AND total_files IS NULL AND reviewed_files IS NULL AND failed_files IS NULL AND skipped_files IS NULL AND excluded_files IS NULL)", name='ck_task_final_counts'),
        CheckConstraint("review_status IS NULL OR (review_status = 'INCOMPLETE' AND failed_files + skipped_files > 0) OR (review_status = 'COMPLETE' AND failed_files + skipped_files = 0 AND reviewed_files > 0) OR (review_status = 'NO_ELIGIBLE_FILES' AND reviewed_files + failed_files + skipped_files = 0)", name='ck_task_coverage'),
        Index('ix_task_created', 'created_at', 'id'),
        Index('ix_task_schedule', 'execution_status', 'created_at', 'id'),
        Index('uq_task_one_running', text('(1)'), unique=True, postgresql_where=text("execution_status = 'running'")),
    )


class ReviewFile(Base):
    __tablename__ = 'review_files'
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey('review_tasks.id', ondelete='CASCADE'), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint('task_id', 'path', name='uq_file_task_path'),
        CheckConstraint("status IN ('reviewed','failed','skipped','excluded')", name='ck_file_status'),
        CheckConstraint("(status = 'reviewed' AND reason_code IS NULL) OR (status <> 'reviewed' AND reason_code IS NOT NULL)", name='ck_file_reason'),
        CheckConstraint("length(path) > 0 AND duration_ms >= 0 AND duration_ms < 'Infinity'::float8", name='ck_file_values'),
    )


class ReviewFinding(Base):
    __tablename__ = 'review_findings'
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    file_id: Mapped[UUID] = mapped_column(ForeignKey('review_files.id', ondelete='CASCADE'), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    line: Mapped[int | None] = mapped_column(Integer)
    suggestion: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        UniqueConstraint('file_id', 'ordinal', name='uq_finding_file_ordinal'),
        CheckConstraint("ordinal >= 0 AND (line IS NULL OR line > 0)", name='ck_finding_location'),
        CheckConstraint("severity IN ('low','medium','high','critical')", name='ck_finding_severity'),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name='ck_finding_evidence'),
    )


class ReviewEventRecord(Base):
    __tablename__ = 'review_events'
    task_id: Mapped[UUID] = mapped_column(ForeignKey('review_tasks.id', ondelete='CASCADE'), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str | None] = mapped_column(String(512))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    termination_reason: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    counters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict] = mapped_column(JSONB, nullable=False)
    api_usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (
        CheckConstraint("seq > 0 AND schema_version = 1 AND duration_ms >= 0 AND duration_ms < 'Infinity'::float8", name='ck_event_values'),
        CheckConstraint("outcome IN ('ok','error','failed','skipped','excluded')", name='ck_event_outcome'),
        CheckConstraint("jsonb_typeof(counters) = 'object' AND jsonb_typeof(limits) = 'object' AND (api_usage IS NULL OR jsonb_typeof(api_usage) = 'object')", name='ck_event_json'),
    )
