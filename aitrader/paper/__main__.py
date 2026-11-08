"""Launch the paper simulator on loopback only.

    python -m paper
"""

from pathlib import Path

import uvicorn

from paper.app import create_app

HERE = Path(__file__).resolve().parent.parent
STATE = HERE / "outputs" / "paper_state.json"


def main():
    app = create_app(STATE)
    # Host is fixed. There is no flag, env var, or config that binds a public interface.
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
