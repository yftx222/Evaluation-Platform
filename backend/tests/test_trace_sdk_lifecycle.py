import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest

from trace_sdk.client import TraceSession


def test_concurrent_sessions_wait_for_final_ack() -> None:
    arrived, release = threading.Event(), threading.Event()
    sealing, acknowledge = threading.Event(), threading.Event()
    lock = threading.Lock()
    batches: dict[str, list[dict[str, Any]]] = {}
    seals: dict[str, str] = {}

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            owner = self.headers["Authorization"]
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result: dict[str, Any]
            if self.path.endswith(":batch"):
                with lock:
                    batches[owner] = payload["events"]
                    if len(batches) == 3:
                        arrived.set()
                assert release.wait(8)
                result = {"accepted": len(payload["events"]), "duplicate": 0}
            else:
                with lock:
                    assert owner in batches and owner not in seals
                    seals[owner] = payload["outcome"]
                    if len(seals) == 3:
                        sealing.set()
                assert acknowledge.wait(8)
                result = {"status": payload["outcome"]}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": result}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise() -> None:
        sessions = {}

        async def agent(name: str) -> str:
            async with TraceSession(
                f"http://127.0.0.1:{server.server_port}", uuid4(), name, timeout=6
            ) as session:
                sessions[name] = session
                assert session.add("FINAL_OUTPUT", name)
                if name == "cancelled":
                    await asyncio.Future[None]()
            assert session.status == "COMPLETE" and session.finalize_confirmed
            assert session.finish().done()
            return "answer"

        normal = asyncio.create_task(agent("normal"))
        cancelled = asyncio.create_task(agent("cancelled"))
        late = asyncio.create_task(agent("late"))
        try:
            assert await asyncio.to_thread(arrived.wait, 5)
            cancelled.cancel()
            assert not normal.done() and not seals
            release.set()
            assert await asyncio.to_thread(sealing.wait, 5)
            assert not normal.done() and not cancelled.done()
            late.cancel()
            acknowledge.set()
            assert await normal == "answer"
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            with pytest.raises(asyncio.CancelledError):
                await late
            assert sessions["late"].status == "PARTIAL"
            assert sessions["late"].confirmed_status == "COMPLETE"
            assert seals == {
                "Bearer normal": "COMPLETE",
                "Bearer cancelled": "PARTIAL",
                "Bearer late": "COMPLETE",
            }
            assert all(events[0]["name"] == owner.split()[1] for owner, events in batches.items())
        finally:
            release.set()
            acknowledge.set()
            for task in (normal, cancelled, late):
                if not task.done():
                    task.cancel()
            await asyncio.gather(normal, cancelled, late, return_exceptions=True)

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
