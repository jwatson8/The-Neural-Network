"""Create database tables for local development and initial deployment."""

from sqlmodel import SQLModel

from src.database import models  # noqa: F401 - registers table metadata
from src.database.database import get_engine


def create_tables() -> None:
    SQLModel.metadata.create_all(get_engine())


if __name__ == "__main__":
    create_tables()
