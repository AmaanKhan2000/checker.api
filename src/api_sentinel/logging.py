import json
import sys
from datetime import UTC, datetime


def event(name: str, **fields):
    print(
        json.dumps({"timestamp": datetime.now(UTC).isoformat(), "event": name, **fields}),
        file=sys.stderr,
        flush=True,
    )
