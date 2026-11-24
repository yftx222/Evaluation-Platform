"""Serve the built console and the real API in an independently restartable test process."""

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.api.main import create_app

api = create_app()
api.mount("/", StaticFiles(directory=Path("../frontend-ios/dist"), html=True), name="console")
