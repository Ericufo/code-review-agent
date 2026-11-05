"""Optional retrieval provenance; no vector extension required for literal mode."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0002_retrieval_event'
down_revision = '0001_review_storage'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('review_events', sa.Column('retrieval', JSONB(), nullable=True))


def downgrade():
    op.drop_column('review_events', 'retrieval')
