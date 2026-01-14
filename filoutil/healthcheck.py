import os
import sys

from sqlalchemy import create_engine, text


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        # In production we expect DATABASE_URL to be present. For dev, we still want healthcheck
        # to fail loudly so orchestration doesn't consider the service healthy.
        print("Missing DATABASE_URL", file=sys.stderr)
        return 1

    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return 0
    except Exception as e:
        print(f"DB healthcheck failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
