from fastapi.testclient import TestClient

from surgiserve import __version__
from surgiserve.app import create_app


def test_health_reports_ok_and_version() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_unknown_route_is_404() -> None:
    client = TestClient(create_app())

    assert client.get("/does-not-exist").status_code == 404


def test_openapi_schema_lists_health() -> None:
    client = TestClient(create_app())

    schema = client.get("/openapi.json").json()

    assert "/health" in schema["paths"]
