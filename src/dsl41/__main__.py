"""`python -m dsl41` (needed by `dsl41 serve`, ss11: textual-serve spawns
`sys.executable -m dsl41 ui --socket <path>` as its per-session command)."""

from dsl41.cli import app

if __name__ == "__main__":
    app()
