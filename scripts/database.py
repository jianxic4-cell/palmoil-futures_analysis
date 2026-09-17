"""Build a PostgreSQL engine on demand; importing this module does not connect."""
import os
from getpass import getpass

from sqlalchemy import URL, create_engine


def database_engine(default_database="quant"):
    password = os.environ.get("PGPASSWORD")
    if password is None:
        password = getpass("PostgreSQL password: ")
    url = URL.create(
        drivername="postgresql+psycopg",
        username=os.environ.get("PGUSER", "postgres"),
        password=password,
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        database=os.environ.get("PGDATABASE", default_database),
    )
    return create_engine(url, hide_parameters=True)

