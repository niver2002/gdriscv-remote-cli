#!/usr/bin/env python3
import base64, json, os, subprocess, sys, time, uuid, threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

JOBS_DIR = Path("/tmp/gdriscv_jobs")

def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

def _now_ms():
    return int(time.time() * 1000)

def _is_within(child, base):
    try:
        child.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False

def _pick_shell_cmd(cmd):
    if Path("/bin/bash").exists():
        return ["/bin/bash", "-lc", cmd]
    return ["/bin/sh", "-lc", cmd]

def _parse_body(body):
    text = body.decode("utf-8", errors="replace").strip()
    if not text: return {}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict): return obj
    except: pass
    if text.startswith("{") and text.endswith("}") and "=" in text:
        inner = text[1:-1].strip()
        out = {}
        for part in inner.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    return {}

def _b64dec(v):
    if not isinstance(v, str) or not v: return None
    try: return base64.b64decode(v.encode("ascii"), validate=False)
    except: return None

def _run_job(job_id, cmd, timeout, cwd, env):
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text("running")
    try:
        with open(d / "stdout", "w") as fo, open(d / "stderr", "w") as fe:
            p = subprocess.Popen(_pick_shell_cmd(cmd), cwd=str(cwd),
                env={**os.environ, **env}, stdout=fo, stderr=fe)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill(); rc = -9
        (d / "exit_code").write_text(str(rc))
        (d / "status").write_text("done")
    except Exception as e:
        (d / "stderr").write_text(str(e))
        (d / "exit_code").write_text("-1")
        (d / "status").write_text("done")

class Handler(BaseHTTPRequestHandler):
    server_version = "gdriscv-agent/0.2"
    def log_message(self, fmt, *a):
        if not getattr(self.server, "quiet", False): super().log_message(fmt, *a)
    @property
    def base_dir(self): return Path(getattr(self.server, "base_dir", os.getcwd()))
    def _json(self, status, obj):
        body = _json_bytes(obj)
        self.send_response(status); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def _bytes(self, status, data, ct="application/octet-stream"):
        self.send_response(status); self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        n = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(n) if n > 0 else b""
    def _resolve(self, raw):
        base = self.base_dir.resolve(); t = (base / raw.lstrip("/")).resolve()
        if not _is_within(t, base): raise ValueError("path escapes base")
        return t

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/health":
            return self._json(200, {"ok":True,"version":"0.2","cwd":str(self.base_dir.resolve()),
                "python":sys.version.split()[0],"time_ms":_now_ms()})
        if p.path == "/ls":
            qs = parse_qs(p.query); raw = (qs.get("path") or [""])[0]
            try:
                t = self._resolve(raw or ".")
                if not t.exists(): return self._json(404,{"ok":False,"error":"not_found"})
                if not t.is_dir(): return self._json(400,{"ok":False,"error":"not_a_directory"})
                entries = [{"name":x.name,"is_dir":x.is_dir(),"size":x.stat().st_size,"mtime":int(x.stat().st_mtime)}
                    for x in sorted(t.iterdir(), key=lambda x:(not x.is_dir(),x.name))]
                return self._json(200,{"ok":True,"path":raw,"entries":entries})
            except Exception as e: return self._json(400,{"ok":False,"error":str(e)})
        if p.path == "/read":
            qs = parse_qs(p.query); raw = (qs.get("path") or [""])[0]
            try:
                t = self._resolve(raw)
                if not t.exists() or not t.is_file(): return self._json(404,{"ok":False,"error":"not_found"})
                return self._json(200,{"ok":True,"path":raw,"content_b64":base64.b64encode(t.read_bytes()).decode()})
            except Exception as e: return self._json(400,{"ok":False,"error":str(e)})
        if p.path == "/download":
            qs = parse_qs(p.query); raw = (qs.get("path") or [""])[0]
            try:
                t = self._resolve(raw)
                if not t.exists() or not t.is_file(): return self._json(404,{"ok":False,"error":"not_found"})
                return self._bytes(200, t.read_bytes())
            except Exception as e: return self._json(400,{"ok":False,"error":str(e)})
        self._json(404,{"ok":False,"error":"not_found"})

    def do_PUT(self):
        p = urlparse(self.path)
        if p.path == "/upload":
            qs = parse_qs(p.query); raw = (qs.get("path") or [""])[0]
            if not raw: return self._json(400,{"ok":False,"error":"missing path"})
            try:
                t = self._resolve(raw); t.parent.mkdir(parents=True,exist_ok=True)
                data = self._body(); t.write_bytes(data)
                return self._json(200,{"ok":True,"path":raw,"bytes":len(data)})
            except Exception as e: return self._json(400,{"ok":False,"error":str(e)})
        self._json(404,{"ok":False,"error":"not_found"})

    def do_POST(self):
        p = urlparse(self.path)
        payload = _parse_body(self._body())

        if p.path == "/upload":
            raw = payload.get("path")
            if not isinstance(raw,str) or not raw: return self._json(400,{"ok":False,"error":"missing path"})
            try:
                t = self._resolve(raw); t.parent.mkdir(parents=True,exist_ok=True)
                data = _b64dec(payload.get("content_b64")) or b""
                t.write_bytes(data); return self._json(200,{"ok":True,"path":raw,"bytes":len(data)})
            except Exception as e: return self._json(400,{"ok":False,"error":str(e)})

        if p.path == "/write":
            raw = payload.get("path")
            if not isinstance(raw,str) or not raw: return self._json(400,{"ok":False,"error":"missing path"})
            data = _b64dec(payload.get("content_b64"))
            if data is None: return self._json(400,{"ok":False,"error":"missing content_b64"})
            try:
                t = self._resolve(raw); t.parent.mkdir(parents=True,exist_ok=True)
                t.write_bytes(data); return self._json(200,{"ok":True,"path":raw,"bytes":len(data)})
            except Exception as e: return self._json(400,{"ok":False,"error":str(e)})

        if p.path == "/exec":
            cmd = payload.get("cmd")
            if not isinstance(cmd,str) or not cmd.strip():
                b = _b64dec(payload.get("cmd_b64"))
                cmd = b.decode("utf-8",errors="replace").strip() if b else ""
            if not cmd: return self._json(400,{"ok":False,"error":"missing cmd"})
            timeout = int(payload.get("timeout_sec",60) or 60)
            cwd = self._resolve(str(payload.get("cwd") or "."))
            env_extra = payload.get("env") or {}
            if not isinstance(env_extra,dict): env_extra = {}
            started = _now_ms()
            try:
                pr = subprocess.run(_pick_shell_cmd(cmd), cwd=str(cwd),
                    env={**os.environ,**{str(k):str(v) for k,v in env_extra.items()}},
                    capture_output=True, timeout=timeout)
                return self._json(200,{"ok":True,"exit_code":pr.returncode,
                    "stdout":pr.stdout.decode("utf-8",errors="replace"),
                    "stderr":pr.stderr.decode("utf-8",errors="replace"),"duration_ms":_now_ms()-started})
            except subprocess.TimeoutExpired as e:
                return self._json(200,{"ok":False,"error":"timeout","duration_ms":_now_ms()-started,
                    "stdout":(e.stdout or b"").decode("utf-8",errors="replace"),
                    "stderr":(e.stderr or b"").decode("utf-8",errors="replace")})
            except Exception as e:
                return self._json(500,{"ok":False,"error":str(e),"duration_ms":_now_ms()-started})

        if p.path == "/async_exec":
            cmd = payload.get("cmd")
            if not isinstance(cmd,str) or not cmd.strip():
                b = _b64dec(payload.get("cmd_b64"))
                cmd = b.decode("utf-8",errors="replace").strip() if b else ""
            if not cmd: return self._json(400,{"ok":False,"error":"missing cmd"})
            timeout = int(payload.get("timeout_sec",3600) or 3600)
            cwd = self._resolve(str(payload.get("cwd") or "."))
            env_extra = payload.get("env") or {}
            if not isinstance(env_extra,dict): env_extra = {}
            job_id = str(uuid.uuid4())[:8]
            t = threading.Thread(target=_run_job, args=(job_id, cmd, timeout, cwd,
                {str(k):str(v) for k,v in env_extra.items()}), daemon=True)
            t.start()
            return self._json(200,{"ok":True,"job_id":job_id,"status":"running"})

        if p.path == "/async_status":
            job_id = payload.get("job_id","")
            if not job_id: return self._json(400,{"ok":False,"error":"missing job_id"})
            d = JOBS_DIR / job_id
            if not d.exists(): return self._json(404,{"ok":False,"error":"job not found"})
            status = (d/"status").read_text().strip() if (d/"status").exists() else "unknown"
            ec = None
            if (d/"exit_code").exists():
                try: ec = int((d/"exit_code").read_text().strip())
                except: ec = -1
            tail = int(payload.get("tail_lines",50) or 50)
            def _tail(f, n):
                if not f.exists(): return ""
                lines = f.read_text(errors="replace").splitlines()
                return "\n".join(lines[-n:])
            return self._json(200,{"ok":True,"status":status,"exit_code":ec,
                "stdout_tail":_tail(d/"stdout",tail),"stderr_tail":_tail(d/"stderr",tail)})

        if p.path == "/tmux/create":
            name = payload.get("name","sess")
            w = int(payload.get("width",200) or 200)
            h = int(payload.get("height",50) or 50)
            subprocess.run(["tmux","kill-session","-t",name], capture_output=True)
            r = subprocess.run(["tmux","new-session","-d","-s",name,"-x",str(w),"-y",str(h)], capture_output=True)
            ok = r.returncode == 0
            return self._json(200,{"ok":ok,"error":"" if ok else r.stderr.decode(errors="replace")})

        if p.path == "/tmux/send":
            name = payload.get("name","sess")
            keys_b64 = _b64dec(payload.get("keys_b64"))
            keys = keys_b64.decode("utf-8",errors="replace") if keys_b64 else payload.get("keys","")
            enter = str(payload.get("enter","true")).lower() != "false"
            args = ["tmux","send-keys","-t",name,keys]
            if enter: args.append("Enter")
            r = subprocess.run(args, capture_output=True)
            return self._json(200,{"ok":r.returncode==0})

        if p.path == "/tmux/capture":
            name = payload.get("name","sess")
            lines = int(payload.get("lines",200) or 200)
            r = subprocess.run(["tmux","capture-pane","-t",name,"-p","-S",f"-{lines}"], capture_output=True)
            return self._json(200,{"ok":True,"output":r.stdout.decode("utf-8",errors="replace")})

        if p.path == "/tmux/kill":
            name = payload.get("name","sess")
            subprocess.run(["tmux","kill-session","-t",name], capture_output=True)
            return self._json(200,{"ok":True})

        self._json(404,{"ok":False,"error":"not_found"})

def main():
    host = os.environ.get("AGENT_HOST","0.0.0.0")
    port = int(os.environ.get("AGENT_PORT","11434"))
    base_dir = os.environ.get("AGENT_BASE_DIR") or os.getcwd()
    quiet = os.environ.get("AGENT_QUIET","0") == "1"
    httpd = ThreadingHTTPServer((host,port), Handler)
    httpd.base_dir = base_dir; httpd.quiet = quiet
    if not quiet: print(f"[agent] listening on http://{host}:{port} (base_dir={Path(base_dir).resolve()})", flush=True)
    try: httpd.serve_forever()
    except KeyboardInterrupt: return 0

if __name__ == "__main__":
    raise SystemExit(main())
