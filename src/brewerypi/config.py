"""Application configuration, read from environment variables.

Values fall back to safe local defaults so the project runs with no
setup. To load a .env file automatically, add `python-dotenv` and call
`load_dotenv()` here.
"""

import os

# Swap to e.g. "postgresql+psycopg://user:pass@host/db" in production.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///app.db")
