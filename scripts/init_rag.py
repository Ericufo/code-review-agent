"""Explicitly create pgvector tables in REVIEW_RAG_DATABASE_URL (never loads .env)."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repository_rag import VectorStore
from review_api.db import create_database_engine


if __name__ == '__main__':
    engine = create_database_engine(os.environ['REVIEW_RAG_DATABASE_URL'])
    try:
        VectorStore(engine).initialize()
        print('Versioned vector storage ready')
    finally:
        engine.dispose()
