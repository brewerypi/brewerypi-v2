"""Application entry point.

Run with `python -m brewerypi.main` or, after `pip install -e .`, the
`brewerypi` command. For now it just creates the database tables from the
models.
"""

from sqlalchemy import create_engine

from brewerypi import models  # noqa: F401  (registers models)
from brewerypi.config import DATABASE_URL
from brewerypi.database import Base


def main() -> None:
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    print(f"Initialized database at {DATABASE_URL}")


if __name__ == "__main__":
    main()
