"""Run the server from a source checkout without installing it (used by Railway)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from splitwise_mcp_server.server import main  # noqa: E402

if __name__ == "__main__":
    main()
