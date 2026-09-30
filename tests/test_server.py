"""End-to-end tests against a real server process streaming the test pattern."""
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tiles  # noqa: E402

KEY = "testkey"


async def receive(ws, timeout):
    """Next message within `timeout` seconds, skipping cursor updates (test mode moves
    a fake cursor constantly) and delay reports. Raises asyncio.TimeoutError if nothing else arrives."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        msg = await ws.receive(timeout=remaining)
        if msg.type == aiohttp.WSMsgType.TEXT and json.loads(msg.data)["t"] in ("c", "cs", "lat"):
            continue
        return msg


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.log = tempfile.TemporaryFile()
        cls.save_dir = Path(tempfile.mkdtemp(prefix="ipd-saved-"))
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--monitor", "test", "--host", "127.0.0.1",
             "--port", str(cls.port), "--key", KEY, "--fps", "30", "--save-dir", str(cls.save_dir)],
            cwd=ROOT, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                return
            except OSError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        shutil.rmtree(cls.save_dir, ignore_errors=True)
        cls.log.seek(0)
        output = cls.log.read().decode(errors="replace")
        cls.log.close()
        if "Traceback" in output:
            sys.stderr.buffer.write(output.encode("utf-8", "replace"))
            raise AssertionError("server logged a traceback (output above)")

    def ws_url(self, key=KEY):
        return f"{self.base}/ws?key={key}"

    def test_upload_saves_files_safely(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                url, name = f"{self.base}/upload", "../../evil name?.txt"
                async with s.post(url, params={"key": KEY, "name": name, "mtime": "1700000000000"},
                                  data=b"hello") as r:
                    self.assertEqual(r.status, 200)
                    first = (await r.json())["name"]
                async with s.post(url, params={"key": KEY, "name": name}, data=b"again") as r:
                    second = (await r.json())["name"]
                async with s.post(url, params={"key": "nope", "name": "x.txt"}, data=b"x") as r:
                    self.assertEqual(r.status, 403)
                async with s.get(f"{self.base}/clipboard", params={"key": "nope"}) as r:
                    self.assertEqual(r.status, 403)
                async with s.post(f"{self.base}/saved", params={"key": "nope"}, json={"names": [first]}) as r:
                    self.assertEqual(r.status, 403)
            return first, second

        first, second = asyncio.run(go())
        self.assertEqual((first, second), ("evil name_.txt", "evil name_ (2).txt"))
        saved = self.save_dir / first
        self.assertEqual(saved.read_bytes(), b"hello")
        self.assertEqual((self.save_dir / second).read_bytes(), b"again")
        self.assertEqual(int(saved.stat().st_mtime), 1700000000)          # the photo keeps its date
        self.assertEqual(sorted(p.name for p in self.save_dir.iterdir()), sorted([first, second]))

    def test_static_files(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                for path, ctype in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css"),
                                    ("/icon.png", "image/png"), ("/manifest.json", "application/json")):
                    async with s.get(self.base + path) as r:
                        self.assertEqual(r.status, 200, path)
                        self.assertIn(ctype, r.headers["Content-Type"], path)
                async with s.get(self.base + "/server.py") as r:
                    self.assertEqual(r.status, 404)
        asyncio.run(go())

    def test_wrong_key_is_rejected(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(self.ws_url("nope")) as ws:
                    msg = await receive(ws, 5)
                    self.assertEqual(msg.type, aiohttp.WSMsgType.CLOSE)
                    self.assertEqual(ws.close_code, 4001)
        asyncio.run(go())

    def test_stream_handshake_and_flow_control(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(self.ws_url()) as ws:
                    hello = json.loads((await receive(ws, 5)).data)
                    self.assertEqual(hello["t"], "hello")
                    size = json.loads((await receive(ws, 5)).data)
                    self.assertEqual((size["t"], size["w"], size["h"]), ("size", 1280, 800))

                    first = await receive(ws, 5)
                    self.assertEqual(first.type, aiohttp.WSMsgType.BINARY)
                    kind, flags, fid, parts = tiles.unpack_frame(first.data)
                    self.assertEqual(kind, tiles.MSG_FRAME)
                    self.assertTrue(flags & tiles.FLAG_KEYFRAME)
                    area = sum(w * h for (x, y, w, h), _ in parts)
                    self.assertEqual(area, 1280 * 800)   # keyframe covers the whole display

                    # Without acks the server must stop after MAX_INFLIGHT frames.
                    frames = [fid]
                    try:
                        while True:
                            msg = await receive(ws, 1.5)
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                frames.append(tiles.unpack_frame(msg.data)[2])
                    except asyncio.TimeoutError:
                        pass
                    self.assertEqual(len(frames), 2)

                    # Acknowledging lets frames flow again.
                    for f in frames:
                        await ws.send_str(json.dumps({"t": "ack", "id": f}))
                    msg = await receive(ws, 3)
                    self.assertEqual(msg.type, aiohttp.WSMsgType.BINARY)
                    self.assertFalse(tiles.unpack_frame(msg.data)[1] & tiles.FLAG_KEYFRAME)

                    await ws.send_str(json.dumps({"t": "ping", "ts": 42}))
                    while True:
                        msg = await receive(ws, 3)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.assertEqual(json.loads(msg.data), {"t": "pong", "ts": 42})
                            break
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
