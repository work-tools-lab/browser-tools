from __future__ import annotations

import http.client
import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import server


SOURCE_ROOT = Path(__file__).absolute().parent.parent


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_depth = 0
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"head", "script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"head", "script", "style"}:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0 and data.strip():
            self.values.append(data.strip())


def zip_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("x", b"zip payload")
    return output.getvalue()


class UploadFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        state_root = SOURCE_ROOT / "local" / ".state"
        state_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=state_root)
        self.root = Path(self.temporary.name)
        (self.root / "docs").mkdir()
        (self.root / "local").mkdir()
        shutil.copyfile(SOURCE_ROOT / "local" / "index.html", self.root / "local" / "index.html")

        self.app = server.UploadApplication(self.root)
        self.httpd = server.create_server(self.app, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(
            server.HOST,
            self.httpd.server_port,
            timeout=5,
        )
        status, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.token = json.loads(config)["token"]

    def tearDown(self) -> None:
        self.connection.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        self.connection.request(method, path, body=body, headers=headers or {})
        response = self.connection.getresponse()
        content = response.read()
        return response.status, content

    def upload(self, entries: list[tuple[str, bytes]]) -> str:
        metadata = json.dumps(
            {"files": [{"name": name, "size": len(content)} for name, content in entries]}
        ).encode()
        status, body = self.request(
            "POST",
            "/api/batches",
            metadata,
            {
                "Content-Type": "application/json",
                "X-Local-Token": self.token,
            },
        )
        self.assertEqual(status, 201, body)
        batch = json.loads(body)

        for position, (_, content) in enumerate(entries, 1):
            status, body = self.request(
                "PUT",
                f"/api/batches/{batch['token']}/{position}",
                content,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Local-Token": self.token,
                },
            )
            self.assertEqual(status, 204, body)

        status, body = self.request(
            "POST",
            f"/api/batches/{batch['token']}/complete",
            b"",
            {"X-Local-Token": self.token},
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)["batch"]

    def test_multiple_then_single_upload(self) -> None:
        entries = [
            ("archive.zip", zip_bytes()),
            ("page.html", b"<!doctype html><title>unchanged</title><script>1</script>"),
            ("notes.md", b"# unchanged\n"),
            ("image.png", b"\x89PNG\r\n\x1a\n\x00\xffbinary"),
        ]
        self.assertEqual(self.upload(entries), "f01")

        first_batch = self.root / "docs" / "f01"
        for position, (original, content) in enumerate(entries, 1):
            anonymous = f"{position:02d}{Path(original).suffix}"
            self.assertEqual((first_batch / anonymous).read_bytes(), content)

        public_html = (self.root / "docs" / "index.html").read_text(encoding="utf-8")
        for original, _ in entries:
            self.assertNotIn(original, public_html)
        self.assertIn('fetch(item.url, { cache: "no-store" })', public_html)
        self.assertIn("response.blob()", public_html)
        self.assertIn("link.download = result.name", public_html)
        self.assertIn("event.preventDefault()", public_html)
        self.assertIn("selected.length === 0", public_html)

        parser = VisibleTextParser()
        parser.feed(public_html)
        self.assertEqual(parser.values, ["01", "02", "03", "04", "OK"])

        mapping = json.loads(
            (self.root / "local" / ".state" / "mappings" / "f01.json").read_text()
        )
        self.assertEqual(
            [item["original"] for item in mapping["files"]],
            [item[0] for item in entries],
        )

        self.assertEqual(self.upload([("one.txt", b"one\r\ntwo\x00")]), "f02")
        self.assertTrue(first_batch.is_dir())
        self.assertEqual((self.root / "docs" / "f02" / "01.txt").read_bytes(), b"one\r\ntwo\x00")
        latest = (self.root / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("./f02/01.txt", latest)
        self.assertNotIn("./f01/", latest)

    def test_local_only_and_multiple_input(self) -> None:
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'type="file" multiple', page)

        status, _ = self.request(
            "POST",
            "/api/batches",
            b'{"files":[]}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_required_extensions_are_generic(self) -> None:
        extensions = [
            ".zip",
            ".html",
            ".txt",
            ".md",
            ".mp4",
            ".png",
            ".jpg",
            ".jpeg",
            ".pdf",
            ".docx",
            ".xlsx",
            ".pptx",
            ".json",
        ]
        for extension in extensions:
            with self.subTest(extension=extension):
                self.assertEqual(server.anonymous_extension(f"original{extension}"), extension)

    def test_next_batch_follows_highest_existing_number(self) -> None:
        (self.root / "docs" / "f03").mkdir()
        batch = self.app.start_batch([{"name": "x.pdf", "size": 0}])
        self.assertEqual(batch["batch"], "f04")


if __name__ == "__main__":
    unittest.main()
