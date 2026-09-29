from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="SurgiServe", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        # Liveness only for now. Milestone 5 adds Postgres and Redis checks.
        return {"status": "ok"}

    return app


app = create_app()
