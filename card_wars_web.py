"""Serve the local Card Wars PDF creator in a browser."""

from email.parser import BytesParser
from email.policy import default
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from urllib.parse import urlsplit
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import webbrowser

from card_wars_pdf import (
    SETS,
    download_deck_pack,
    download_set_pack,
    fetch_decks,
    make_pdf,
    resolve_set,
    safe_filename,
)


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
PACKS = {}
PACKS_LOCK = threading.Lock()
PACK_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def cleanup_packs():
    with PACKS_LOCK:
        packs = list(PACKS.values())
        PACKS.clear()
    for pack in packs:
        shutil.rmtree(pack["directory"], ignore_errors=True)


class CardWarsWebHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, status, content_type, data, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status, message):
        data = json.dumps({"error": message}).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/tokens.pdf":
            token_pdf = ROOT / "tokens_printable_2sidedPrint.pdf"
            try:
                data = token_pdf.read_bytes()
            except OSError as exc:
                self.log_error("Could not read printable tokens PDF: %s", exc)
                self._send_error_json(500, "Could not read the printable tokens PDF.")
                return
            self._send_bytes(
                200,
                "application/pdf",
                data,
                'attachment; filename="tokens_printable_2sidedPrint.pdf"',
            )
            return

        if path == "/api/sets":
            self._send_json(200, {"sets": SETS})
            return
        if path == "/api/decks":
            try:
                decks = fetch_decks()
            except Exception as exc:
                self.log_error("Could not load Card Dweeb decks: %s", exc)
                self._send_error_json(502, f"Could not load Card Dweeb decks: {exc}")
                return
            self._send_json(200, {"decks": decks})
            return

        pack_match = re.fullmatch(
            r"/api/packs/([0-9a-f]{32})/(archive|images/([0-9]+))", path
        )
        if pack_match:
            token, resource, image_number = pack_match.groups()
            with PACKS_LOCK:
                pack = PACKS.get(token)
            if not pack:
                self._send_error_json(404, "This download is no longer available.")
                return
            if resource == "archive":
                file_path = pack["archive"]
                content_type = "application/zip"
                filename = pack["archive_name"]
                disposition = f'attachment; filename="{filename}"'
            else:
                index = int(image_number)
                if index >= len(pack["images"]):
                    self._send_error_json(404, "Card image not found.")
                    return
                file_path = pack["images"][index]
                content_type = "image/" + (
                    "jpeg" if file_path.suffix.lower() in (".jpg", ".jpeg")
                    else file_path.suffix.lower().lstrip(".")
                )
                disposition = "inline"
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                self.log_error("Could not read downloaded card file: %s", exc)
                self._send_error_json(500, "Could not read downloaded card file.")
                return
            self._send_bytes(200, content_type, data, disposition)
            return

        assets = {
            "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
            "/background.webp": (
                ROOT / "adventure-time-card-wars-episode-image.png",
                "image/webp",
            ),
        }
        asset = assets.get(path)
        if not asset:
            self._send_error_json(404, "Not found.")
            return

        path, content_type = asset
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_error_json(500, f"Could not read web asset: {exc}")
            return
        self._send_bytes(200, content_type, data)

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", data)

    def _read_json(self, max_size=16 * 1024):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request size.") from None
        if content_length <= 0 or content_length > max_size:
            raise ValueError("Request body is empty or too large.")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Expected a JSON request.")
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return payload

    def _resolve_pack_images(self, tokens):
        if not isinstance(tokens, list) or any(
            not isinstance(token, str) or not PACK_TOKEN_RE.fullmatch(token)
            for token in tokens
        ):
            raise ValueError("Invalid downloaded card selection.")
        image_paths = []
        with PACKS_LOCK:
            for token in tokens:
                pack = PACKS.get(token)
                if not pack:
                    raise ValueError(
                        "A downloaded card set or deck has expired. Download "
                        "it again before creating the PDF."
                    )
                image_paths.extend(pack["images"])
        return image_paths

    def _send_pdf(self, image_paths, settings):
        if not image_paths:
            raise ValueError("Choose or download at least one card image.")
        with tempfile.TemporaryDirectory(prefix="card-wars-pdf-") as temp_dir:
            output_path = Path(temp_dir) / "card_wars_print.pdf"
            make_pdf(
                image_paths,
                str(output_path),
                settings["width"],
                settings["height"],
                settings["margin"],
                settings["gap"],
                settings["orientation"],
                settings["fit"],
                settings["cut_lines"],
                settings["quality_reduction"],
                settings.get("auto_detect", False),
            )

            pdf_size = output_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="card_wars_print.pdf"',
            )
            self.send_header("Content-Length", str(pdf_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with output_path.open("rb") as pdf_file:
                while True:
                    chunk = pdf_file.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path in ("/api/download-set", "/api/download-deck"):
            self._download_pack(path)
            return
        if path == "/create-pdf-from-packs":
            try:
                request = self._read_json()
                settings = request["settings"]
                image_paths = self._resolve_pack_images(request["pack_tokens"])
                self._send_pdf(image_paths, settings)
            except (
                KeyError, TypeError, ValueError, UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                self._send_error_json(400, str(exc))
            except Exception as exc:
                self.log_error("PDF creation from downloaded cards failed: %s", exc)
                self._send_error_json(500, f"PDF creation failed: {exc}")
            return
        if path != "/create-pdf":
            self._send_error_json(404, "Not found.")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json(400, "Invalid request size.")
            return
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            self._send_error_json(
                413, "Upload must be between 1 byte and 256 MB."
            )
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            self._send_error_json(400, "Expected a multipart image upload.")
            return

        try:
            body = self.rfile.read(content_length)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode("ascii") +
                b"\r\nMIME-Version: 1.0\r\n\r\n" + body
            )
            if not message.is_multipart():
                raise ValueError("The uploaded form could not be read.")

            settings = None
            uploads = []
            pack_tokens = []
            for part in message.iter_parts():
                field_name = part.get_param(
                    "name", header="content-disposition"
                )
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if field_name == "settings":
                    settings = json.loads(payload.decode("utf-8"))
                elif field_name == "pack_tokens":
                    pack_tokens = json.loads(payload.decode("utf-8"))
                elif field_name == "images" and filename and payload:
                    uploads.append((filename, payload))

            image_paths = self._resolve_pack_images(pack_tokens)
            if not uploads and not image_paths:
                raise ValueError("Choose at least one image to create a PDF.")
            if settings is None:
                raise ValueError("PDF settings were not provided.")

            with tempfile.TemporaryDirectory(prefix="card-wars-") as temp_dir:
                image_paths = []
                for index, (filename, data) in enumerate(uploads):
                    suffix = Path(filename).suffix.lower()
                    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        suffix = ".img"
                    image_name = safe_filename(Path(filename).name)
                    image_path = os.path.join(
                        temp_dir, f"card-{index}-{image_name}"
                    )
                    with open(image_path, "wb") as image_file:
                        image_file.write(data)
                    image_paths.append(image_path)

                self._send_pdf(image_paths, settings)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_error_json(400, str(exc))
        except Exception as exc:
            self.log_error("PDF creation failed: %s", exc)
            self._send_error_json(500, f"PDF creation failed: {exc}")

    def _download_pack(self, path):
        temp_dir = None
        try:
            request = self._read_json()
            temp_dir = tempfile.mkdtemp(prefix="card-wars-pack-")
            if path == "/api/download-set":
                set_name = request.get("set")
                if not isinstance(set_name, str) or not resolve_set(set_name):
                    raise ValueError("Choose a valid card set.")
                archive_name = (
                    safe_filename(set_name).replace(" ", "_") + "_cards.zip"
                )
                archive_path = Path(temp_dir) / archive_name
                pack_name, images, failures = download_set_pack(
                    set_name, str(archive_path)
                )
                pack_type = "set"
            else:
                deck_id = request.get("id")
                deck_name = request.get("name")
                if (
                    isinstance(deck_id, bool)
                    or not isinstance(deck_id, int)
                    or deck_id <= 0
                    or not isinstance(deck_name, str)
                    or not deck_name.strip()
                ):
                    raise ValueError("Choose a valid deck.")
                pack_name = deck_name.strip()[:160]
                archive_name = (
                    safe_filename(pack_name).replace(" ", "_") + "_cards.zip"
                )
                archive_path = Path(temp_dir) / archive_name
                pack_name, images, failures = download_deck_pack(
                    deck_id, pack_name, str(archive_path)
                )
                pack_type = "deck"

            if not images:
                raise ValueError(
                    f"No card images were downloaded for {pack_name}."
                )
            token = uuid.uuid4().hex
            with PACKS_LOCK:
                PACKS[token] = {
                    "directory": temp_dir,
                    "archive": archive_path,
                    "archive_name": archive_name,
                    "images": [Path(image) for image in images],
                }
            self._send_json(200, {
                "token": token,
                "name": pack_name,
                "type": pack_type,
                "images": len(images),
                "failures": failures,
            })
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self._send_error_json(400, str(exc))
        except Exception as exc:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self.log_error("Card Dweeb download failed: %s", exc)
            self._send_error_json(502, f"Card Dweeb download failed: {exc}")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), CardWarsWebHandler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Card Wars PDF Creator is running at {url}")
    print("Press Ctrl+C to stop the local web server.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Card Wars PDF Creator.")
    finally:
        server.server_close()
        cleanup_packs()


if __name__ == "__main__":
    main()
