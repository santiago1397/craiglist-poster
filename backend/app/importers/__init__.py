"""On-demand command-line importers, run inside the API container.

    docker compose exec api python -m app.importers.companycam_import --help
    docker compose exec api python -m app.importers.curate --help

They live under `app/` rather than in the repo's `scripts/` directory because
`backend/Dockerfile` copies only `app`, `alembic`, `alembic.ini` and
`pyproject.toml` into the image — nothing in `scripts/` exists at runtime, and
these need both the database and the `/var/lib/cl/images` volume, which only
exist inside the container.

`argparse`, not typer: typer is a dependency of the desktop poster's
`pyproject.toml`, not the backend's.
"""
