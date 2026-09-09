#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_FILES = 999
MAX_JSON_BYTES = 1_000_000
BATCH_PATTERN = re.compile(r"f([0-9]+)\Z")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}\Z")


class UploadError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def anonymous_extension(filename: str) -> str:
    if not filename or "\x00" in filename:
        raise UploadError("Invalid file name")

    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(basename).suffix

    if not suffix:
        return ""
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
        raise UploadError("Invalid file extension")
    return suffix


def render_public_index(batch: str | None, names: list[str]) -> str:
    labels = []
    items = []

    for offset, name in enumerate(names):
        number = f"{offset + 1:02d}"
        labels.append(
            f'        <label><input type="checkbox" value="{offset}"> '
            f"<span>{number}</span></label>"
        )
        items.append({"url": f"./{batch}/{name}", "name": name})

    item_markup = "\n".join(labels)
    item_json = json.dumps(items, ensure_ascii=True, separators=(",", ":"))
    disabled = "" if names else " disabled"

    return f'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>01</title>
    <style>
      :root {{
        color-scheme: light dark;
        font-family: system-ui, sans-serif;
      }}

      body {{
        display: grid;
        min-height: 100vh;
        min-height: 100dvh;
        margin: 0;
        place-items: center;
      }}

      form,
      #items {{
        display: grid;
        gap: 1rem;
      }}

      label {{
        display: flex;
        gap: 0.75rem;
        align-items: center;
        font-variant-numeric: tabular-nums;
      }}

      input,
      button {{
        min-width: 1.25rem;
        min-height: 1.25rem;
      }}

      button {{
        margin-top: 0.5rem;
        padding: 0.5rem 1.5rem;
      }}
    </style>
  </head>
  <body>
    <form id="selection">
      <div id="items">
{item_markup}
      </div>
      <button type="submit"{disabled}>OK</button>
    </form>
    <script>
      const items = {item_json};
      const form = document.querySelector("#selection");
      const button = form.querySelector("button");

      form.addEventListener("submit", async (event) => {{
        event.preventDefault();
        const selected = [...form.querySelectorAll("input:checked")];

        if (selected.length === 0) {{
          return;
        }}

        button.disabled = true;

        try {{
          const results = await Promise.all(selected.map(async (input) => {{
            const item = items[Number(input.value)];
            const response = await fetch(item.url, {{ cache: "no-store" }});

            if (!response.ok) {{
              throw new Error(String(response.status));
            }}

            return {{ blob: await response.blob(), name: item.name }};
          }}));

          for (const result of results) {{
            const url = URL.createObjectURL(result.blob);
            const link = document.createElement("a");
            link.hidden = true;
            link.href = url;
            link.download = result.name;
            document.body.append(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }}
        }} finally {{
          button.disabled = false;
        }}
      }});
    </script>
  </body>
</html>
'''


class UploadApplication:
    def __init__(self, repository_root: Path):
        self.root = repository_root.absolute()
        self.docs = self.root / "docs"
        self.local = self.root / "local"
        self.state = self.local / ".state"
        self.sessions_dir = self.state / "sessions"
        self.mappings_dir = self.state / "mappings"
        self.ui_path = self.local / "index.html"
        self.lock = threading.Lock()
        self.sessions: dict[str, dict[str, object]] = {}
        self.reserved_batches: set[str] = set()
        self.request_token = secrets.token_urlsafe(32)

        for required in (self.docs, self.local):
            if required.is_symlink() or not required.is_dir():
                raise RuntimeError(f"Invalid directory: {required.name}")

        if self.state.is_symlink():
            raise RuntimeError("Invalid local state directory")
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.mappings_dir.mkdir(parents=True, exist_ok=True)

    def _next_batch(self) -> str:
        numbers = []

        for reserved in self.reserved_batches:
            match = BATCH_PATTERN.fullmatch(reserved)
            if match:
                numbers.append(int(match.group(1)))

        for entry in self.docs.iterdir():
            match = BATCH_PATTERN.fullmatch(entry.name)
            if match:
                numbers.append(int(match.group(1)))

        for entry in self.mappings_dir.iterdir():
            match = re.fullmatch(r"f([0-9]+)\.json", entry.name)
            if match:
                numbers.append(int(match.group(1)))

        number = max(numbers, default=0) + 1
        return f"f{number:02d}"

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                output.write(value)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def start_batch(self, files: object) -> dict[str, object]:
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise UploadError("Select between 1 and 999 files")

        prepared = []
        for offset, item in enumerate(files):
            if not isinstance(item, dict):
                raise UploadError("Invalid file list")
            original = item.get("name")
            size = item.get("size")
            if not isinstance(original, str) or isinstance(size, bool) or not isinstance(size, int):
                raise UploadError("Invalid file metadata")
            if size < 0 or size > 9_007_199_254_740_991:
                raise UploadError("Invalid file size")

            number = f"{offset + 1:02d}"
            anonymous = f"{number}{anonymous_extension(original)}"
            prepared.append(
                {
                    "number": number,
                    "anonymous": anonymous,
                    "original": original,
                    "size": size,
                }
            )

        with self.lock:
            batch = self._next_batch()
            self.reserved_batches.add(batch)
            token = secrets.token_urlsafe(24)
            session_dir = self.sessions_dir / token

            try:
                public_dir = session_dir / "public"
                public_dir.mkdir(parents=True)
                session = {
                    "batch": batch,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "files": prepared,
                    "session_dir": session_dir,
                    "public_dir": public_dir,
                }
                self._write_json(
                    session_dir / "session.json",
                    {
                        "batch": batch,
                        "created_at": session["created_at"],
                        "files": prepared,
                    },
                )
                self.sessions[token] = session
            except Exception:
                self.reserved_batches.discard(batch)
                shutil.rmtree(session_dir, ignore_errors=True)
                raise

        return {"token": token, "batch": batch, "count": len(prepared)}

    def store_file(self, token: str, position: int, stream: BinaryIO, length: int) -> None:
        if not TOKEN_PATTERN.fullmatch(token):
            raise UploadError("Invalid upload token")

        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                raise UploadError("Unknown upload", HTTPStatus.NOT_FOUND)
            files = session["files"]
            assert isinstance(files, list)
            if not 1 <= position <= len(files):
                raise UploadError("Invalid file number")
            item = files[position - 1]
            assert isinstance(item, dict)
            expected = item["size"]
            anonymous = item["anonymous"]
            public_dir = session["public_dir"]
            assert isinstance(expected, int)
            assert isinstance(anonymous, str)
            assert isinstance(public_dir, Path)

        if length != expected:
            raise UploadError("File size does not match")

        destination = public_dir / anonymous
        temporary = public_dir / f".{position:02d}.{secrets.token_hex(8)}.tmp"
        remaining = length

        try:
            with temporary.open("xb") as output:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise UploadError("Upload ended early")
                    output.write(chunk)
                    remaining -= len(chunk)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def complete_batch(self, token: str) -> dict[str, object]:
        if not TOKEN_PATTERN.fullmatch(token):
            raise UploadError("Invalid upload token")

        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                raise UploadError("Unknown upload", HTTPStatus.NOT_FOUND)

            batch = session["batch"]
            files = session["files"]
            session_dir = session["session_dir"]
            public_dir = session["public_dir"]
            assert isinstance(batch, str)
            assert isinstance(files, list)
            assert isinstance(session_dir, Path)
            assert isinstance(public_dir, Path)

            for item in files:
                assert isinstance(item, dict)
                path = public_dir / str(item["anonymous"])
                if not path.is_file() or path.stat().st_size != item["size"]:
                    raise UploadError("Upload is incomplete")

            destination = self.docs / batch
            if destination.exists() or destination.is_symlink():
                raise UploadError("Batch already exists", HTTPStatus.CONFLICT)

            names = [str(item["anonymous"]) for item in files]
            public_index = render_public_index(batch, names)
            mapping_path = self.mappings_dir / f"{batch}.json"
            if mapping_path.exists() or mapping_path.is_symlink():
                raise UploadError("Mapping already exists", HTTPStatus.CONFLICT)

            public_dir.replace(destination)
            self._write_json(
                mapping_path,
                {
                    "batch": batch,
                    "created_at": session["created_at"],
                    "files": files,
                },
            )
            self._write_text(self.docs / "index.html", public_index)

            (session_dir / "session.json").unlink(missing_ok=True)
            session_dir.rmdir()
            del self.sessions[token]
            self.reserved_batches.discard(batch)

        return {"batch": batch, "count": len(files)}


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: UploadApplication):
        self.app = app
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: LocalHTTPServer

    def _host_is_local(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        return host in {"127.0.0.1", "localhost"}

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Local-Token", "")
        return secrets.compare_digest(supplied, self.server.app.request_token)

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error(self, error: UploadError) -> None:
        self._send_json(error.status, {"error": str(error)})

    def _read_json(self) -> object:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise UploadError("Expected JSON")
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise UploadError("Missing content length")
        try:
            length = int(length_text)
        except ValueError as error:
            raise UploadError("Invalid content length") from error
        if not 0 <= length <= MAX_JSON_BYTES:
            raise UploadError("Invalid content length")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UploadError("Invalid JSON") from error

    def _prepare(self, require_token: bool = False) -> bool:
        if not self._host_is_local():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Local access only"})
            return False
        if require_token and not self._authorized():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid local token"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._prepare():
            return
        if self.path in {"/", "/index.html"}:
            body = self.server.app.ui_path.read_bytes()
            self._send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._send_json(HTTPStatus.OK, {"token": self.server.app.request_token})
        elif self.path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._prepare(require_token=True):
            return
        try:
            if self.path == "/api/batches":
                request = self._read_json()
                if not isinstance(request, dict):
                    raise UploadError("Invalid request")
                result = self.server.app.start_batch(request.get("files"))
                self._send_json(HTTPStatus.CREATED, result)
                return

            match = re.fullmatch(r"/api/batches/([A-Za-z0-9_-]+)/complete", self.path)
            if match:
                result = self.server.app.complete_batch(match.group(1))
                self._send_json(HTTPStatus.OK, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except UploadError as error:
            self._send_error(error)
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error"})

    def do_PUT(self) -> None:
        if not self._prepare(require_token=True):
            return
        try:
            match = re.fullmatch(r"/api/batches/([A-Za-z0-9_-]+)/([0-9]+)", self.path)
            if not match:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            length_text = self.headers.get("Content-Length")
            if length_text is None:
                raise UploadError("Missing content length")
            try:
                length = int(length_text)
            except ValueError as error:
                raise UploadError("Invalid content length") from error
            if length < 0:
                raise UploadError("Invalid content length")
            self.server.app.store_file(match.group(1), int(match.group(2)), self.rfile, length)
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "application/octet-stream")
        except UploadError as error:
            self._send_error(error)
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error"})

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        print(f"{self.client_address[0]} {message}")


def create_server(app: UploadApplication, port: int) -> LocalHTTPServer:
    return LocalHTTPServer((HOST, port), app)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local uploader")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")

    root = Path(__file__).absolute().parent.parent
    app = UploadApplication(root)
    server = create_server(app, arguments.port)
    print(f"http://{HOST}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
