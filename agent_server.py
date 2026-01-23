#!/usr/bin/env python3
import base64
import json
import os
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_within(child: Path, base: Path) -> bool:
    try:
        child.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def _pick_shell_cmd(cmd: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-Command", cmd]

    if Path("/bin/bash").exists():
        return ["/bin/bash", "-lc", cmd]
    return ["/bin/sh", "-lc", cmd]


def _parse_body_as_object(body: bytes) -> dict:
    """
    The gdriscv "remote" gateway appears to parse JSON and then forward a Java Map-like
    string (e.g. "{cmd=pwd, timeout_sec=30}") to port 18080.

    Accept a few common formats:
    - JSON object: {"cmd":"pwd"}
    - Java Map.toString(): {cmd=pwd, timeout_sec=30}
    - Querystring: cmd=pwd&timeout_sec=30
    """
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    if text.startswith("{") and text.endswith("}") and "=" in text:
        inner = text[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, str] = {}
        for part in inner.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
        return out

    if "=" in text and "&" in text:
        qs = parse_qs(text, keep_blank_values=True)
        return {k: (v[-1] if v else "") for k, v in qs.items()}

    return {}


def _b64_decode_maybe(value) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value.encode("ascii"), validate=False)
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "gdriscv-agent/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    @property
    def base_dir(self) -> Path:
        return Path(getattr(self.server, "base_dir", os.getcwd()))

    def _send_json(self, status: int, obj) -> None:
        body = _json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _resolve_path(self, raw_path: str) -> Path:
        base = self.base_dir.resolve()
        rel = raw_path.lstrip("/")
        target = (base / rel).resolve()
        if not _is_within(target, base):
            raise ValueError("path escapes base dir")
        return target

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "cwd": str(self.base_dir.resolve()),
                    "python": sys.version.split()[0],
                    "time_ms": _now_ms(),
                },
            )

        if parsed.path == "/ls":
            qs = parse_qs(parsed.query)
            raw = (qs.get("path") or [""])[0]
            try:
                target = self._resolve_path(raw or ".")
                if not target.exists():
                    return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                if not target.is_dir():
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "not_a_directory"})
                entries = []
                for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
                    st = p.stat()
                    entries.append(
                        {
                            "name": p.name,
                            "is_dir": p.is_dir(),
                            "size": st.st_size,
                            "mtime": int(st.st_mtime),
                        }
                    )
                return self._send_json(HTTPStatus.OK, {"ok": True, "path": str(raw), "entries": entries})
            except Exception as e:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})

        if parsed.path == "/read":
            qs = parse_qs(parsed.query)
            raw = (qs.get("path") or [""])[0]
            try:
                target = self._resolve_path(raw)
                if not target.exists() or not target.is_file():
                    return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                data = target.read_bytes()
                return self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "path": raw, "content_b64": base64.b64encode(data).decode("ascii")},
                )
            except Exception as e:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})

        if parsed.path == "/download":
            qs = parse_qs(parsed.query)
            raw = (qs.get("path") or [""])[0]
            try:
                target = self._resolve_path(raw)
                if not target.exists() or not target.is_file():
                    return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                data = target.read_bytes()
                return self._send_bytes(HTTPStatus.OK, data)
            except Exception as e:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})

        return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/upload":
            return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        qs = parse_qs(parsed.query)
        raw = (qs.get("path") or [""])[0]
        if not raw:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing path"})

        try:
            target = self._resolve_path(raw)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = self._read_body()
            target.write_bytes(data)
            return self._send_json(HTTPStatus.OK, {"ok": True, "path": raw, "bytes": len(data)})
        except Exception as e:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            payload = _parse_body_as_object(self._read_body())
            raw = payload.get("path")
            if not isinstance(raw, str) or not raw:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing path"})

            try:
                target = self._resolve_path(raw)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = _b64_decode_maybe(payload.get("content_b64"))
                if data is None:
                    data = b""
                target.write_bytes(data)
                return self._send_json(HTTPStatus.OK, {"ok": True, "path": raw, "bytes": len(data)})
            except Exception as e:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})

        if parsed.path == "/write":
            payload = _parse_body_as_object(self._read_body())
            raw = payload.get("path")
            if not isinstance(raw, str) or not raw:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing path"})

            try:
                data = _b64_decode_maybe(payload.get("content_b64"))
                if data is None:
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing/invalid content_b64"})
                target = self._resolve_path(raw)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                return self._send_json(HTTPStatus.OK, {"ok": True, "path": raw, "bytes": len(data)})
            except Exception as e:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})

        if parsed.path == "/exec":
            payload = _parse_body_as_object(self._read_body())
            cmd = payload.get("cmd")
            if not isinstance(cmd, str) or not cmd.strip():
                cmd_b64 = _b64_decode_maybe(payload.get("cmd_b64"))
                cmd = cmd_b64.decode("utf-8", errors="replace").strip() if cmd_b64 else ""
            if not cmd:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing cmd/cmd_b64"})

            timeout = payload.get("timeout_sec", 60)
            try:
                timeout = int(timeout)
            except Exception:
                timeout = 60

            cwd_raw = payload.get("cwd") or "."
            try:
                cwd = self._resolve_path(str(cwd_raw))
            except Exception as e:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad cwd: {e}"})

            env_extra = payload.get("env") or {}
            if not isinstance(env_extra, dict):
                env_extra = {}

            stdin_b64 = payload.get("stdin_b64")
            stdin_data = None
            if isinstance(stdin_b64, str) and stdin_b64:
                try:
                    stdin_data = base64.b64decode(stdin_b64.encode("ascii"), validate=False)
                except Exception:
                    stdin_data = None

            started = _now_ms()
            try:
                p = subprocess.run(
                    _pick_shell_cmd(cmd),
                    cwd=str(cwd),
                    env={**os.environ, **{str(k): str(v) for k, v in env_extra.items()}},
                    input=stdin_data,
                    capture_output=True,
                    timeout=timeout,
                )
                duration = _now_ms() - started
                return self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "exit_code": p.returncode,
                        "stdout": p.stdout.decode("utf-8", errors="replace"),
                        "stderr": p.stderr.decode("utf-8", errors="replace"),
                        "duration_ms": duration,
                    },
                )
            except subprocess.TimeoutExpired as e:
                duration = _now_ms() - started
                return self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": False,
                        "error": "timeout",
                        "duration_ms": duration,
                        "stdout": (e.stdout or b"").decode("utf-8", errors="replace"),
                        "stderr": (e.stderr or b"").decode("utf-8", errors="replace"),
                    },
                )
            except Exception as e:
                duration = _now_ms() - started
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(e), "duration_ms": duration})

        return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})


def main() -> int:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "18080"))
    base_dir = os.environ.get("AGENT_BASE_DIR") or os.getcwd()
    quiet = os.environ.get("AGENT_QUIET", "0") == "1"

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.base_dir = base_dir
    httpd.quiet = quiet

    # Some web terminals only display stdout. Use stdout + flush for startup logs.
    if not quiet:
        print(f"[agent] listening on http://{host}:{port} (base_dir={Path(base_dir).resolve()})", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
