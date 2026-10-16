"""`python -m dsl41`; `dsl41 serve` (ss11) spawns each session as `-m dsl41 ui --socket <path>`."""

from dsl41.cli import app

if __name__ == "__main__":
    app()
