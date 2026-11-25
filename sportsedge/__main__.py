import sys

try:
    from .cli import main

    main()
except ModuleNotFoundError as e:
    # run with a Python that lacks the project's libraries (for example plain `python` instead of the .venv one)
    if (e.name or "").split(".")[0] in {"numpy", "pandas", "scipy", "sklearn", "requests", "fastapi", "uvicorn", "pydantic",
                                        "nflreadpy", "nba_api", "rich", "pyarrow", "polars"}:
        args = " ".join(sys.argv[1:])
        sys.exit(f"This Python does not have the project's libraries ('{e.name}' is missing).\n"
                 f"Run it with the project's own Python instead:\n\n    .\\sportsedge {args}\n\n"
                 f"(that is short for  .\\.venv\\Scripts\\python -m sportsedge {args})")
    raise
