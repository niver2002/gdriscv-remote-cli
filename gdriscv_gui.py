"""gdriscv Remote AI Dev GUI v2 - Ollama channel only (port 11434, no rate limit)."""
import base64, json, os, re, threading, time, traceback, tkinter as tk
from tkinter import ttk, scrolledtext
import requests

LOGFILE = os.path.join(os.path.expanduser("~"), "gdriscv_debug.log")

def _dbg(msg):
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

class RemoteAPI:
    """All calls go through Ollama channel (https://xxx.gdriscv.com -> device:11434)."""
    def __init__(self, api_key: str, ollama_url: str):
        self.base = ollama_url.rstrip("/")
        self.headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        self.timeout = 120

    def _url(self, path): return f"{self.base}{path}"

    def _unwrap(self, resp):
        resp.raise_for_status()
        return resp.json()

    def health(self):
        return self._unwrap(requests.get(self._url("/health"), headers=self.headers, timeout=self.timeout))

    def exec(self, cmd, timeout_sec=60):
        body = {"cmd_b64": base64.b64encode(cmd.encode()).decode(), "timeout_sec": timeout_sec}
        return self._unwrap(requests.post(self._url("/exec"), headers=self.headers, json=body, timeout=self.timeout))

    def write_file(self, path, content):
        body = {"path": path, "content_b64": base64.b64encode(content.encode()).decode()}
        return self._unwrap(requests.post(self._url("/write"), headers=self.headers, json=body, timeout=self.timeout))

    def read_file(self, path):
        r = self._unwrap(requests.get(self._url("/read"), headers=self.headers, params={"path": path}, timeout=self.timeout))
        return base64.b64decode(r.get("content_b64", "")).decode(errors="replace")

    def async_exec(self, cmd, timeout_sec=3600):
        body = {"cmd_b64": base64.b64encode(cmd.encode()).decode(), "timeout_sec": timeout_sec}
        return self._unwrap(requests.post(self._url("/async_exec"), headers=self.headers, json=body, timeout=self.timeout))

    def async_status(self, job_id, tail_lines=50):
        body = {"job_id": job_id, "tail_lines": tail_lines}
        return self._unwrap(requests.post(self._url("/async_status"), headers=self.headers, json=body, timeout=self.timeout))

    def tmux_create(self, name, width=200, height=50):
        body = {"name": name, "width": width, "height": height}
        return self._unwrap(requests.post(self._url("/tmux/create"), headers=self.headers, json=body, timeout=self.timeout))

    def tmux_send(self, name, keys, enter=True):
        body = {"name": name, "keys_b64": base64.b64encode(keys.encode()).decode(), "enter": enter}
        return self._unwrap(requests.post(self._url("/tmux/send"), headers=self.headers, json=body, timeout=self.timeout))

    def tmux_capture(self, name, lines=200):
        body = {"name": name, "lines": lines}
        return self._unwrap(requests.post(self._url("/tmux/capture"), headers=self.headers, json=body, timeout=self.timeout))

    def tmux_kill(self, name):
        body = {"name": name}
        return self._unwrap(requests.post(self._url("/tmux/kill"), headers=self.headers, json=body, timeout=self.timeout))

class TmuxSession:
    def __init__(self, api, name):
        self.api, self.name = api, name
    def create(self):
        self.api.tmux_create(self.name)
    def send_keys(self, text):
        self.api.tmux_send(self.name, text)
    def capture(self, lines=200):
        r = self.api.tmux_capture(self.name, lines)
        return r.get("output", "")
    def kill(self):
        self.api.tmux_kill(self.name)

def strip_ansi(text):
    return re.sub(r'\x1b[\[\(][0-9;]*[a-zA-Z]|\x1b[=>]|\r', '', text)

AGENT_INSTALL_CMD = (
    'curl -fsSL https://raw.githubusercontent.com/niver2002/gdriscv-remote-cli/main/agent_server.py'
    ' -o ~/agent_server.py && pkill -f "python3.*agent_server" 2>/dev/null;'
    ' AGENT_HOST=0.0.0.0 AGENT_PORT=11434 AGENT_BASE_DIR="$HOME"'
    ' nohup python3 ~/agent_server.py > /tmp/agent.log 2>&1 &'
)

CONF_FILE = os.path.join(os.path.expanduser("~"), ".gdriscv_gui.json")

def _load_conf():
    try:
        with open(CONF_FILE, "r") as f: return json.load(f)
    except: return {}

def _save_conf(d):
    old = _load_conf(); old.update(d)
    with open(CONF_FILE, "w") as f: json.dump(old, f)

class GdriscvGUI:
    def __init__(self):
        self.api = None
        self.sessions = {}
        self.poll_running = {}
        self._bashrc_exports = ""
        self.root = tk.Tk()
        self.root.title("gdriscv Remote AI Dev v2")
        self.root.geometry("960x660")
        self.root.configure(bg="#1e1e1e")
        s = ttk.Style(); s.theme_use("clam")
        for w in [".", "TLabel", "TFrame"]:
            s.configure(w, background="#1e1e1e", foreground="#d4d4d4")
        s.configure("TLabel", font=("Consolas", 11))
        s.configure("TButton", font=("Consolas", 11))
        s.configure("TEntry", fieldbackground="#2d2d2d", foreground="#e0e0e0",
                     insertcolor="#e0e0e0", font=("Consolas", 11))
        s.configure("TNotebook", background="#1e1e1e")
        s.configure("TNotebook.Tab", font=("Consolas", 11), padding=[12, 4])
        s.configure("Header.TLabel", font=("Consolas", 14, "bold"), foreground="#569cd6")
        s.configure("Status.TLabel", font=("Consolas", 10), foreground="#ce9178")
        s.configure("Hint.TLabel", font=("Consolas", 9), foreground="#6a9955")
        self.container = ttk.Frame(self.root, padding=12)
        self.container.pack(fill="both", expand=True)
        self.build_stage1()

    def _clear(self):
        for w in self.container.winfo_children(): w.destroy()
    def _bg(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()
    def _after(self, fn, *a):
        self.root.after(0, fn, *a)

    # ── Stage 1: Connect ──
    def build_stage1(self):
        self._clear()
        conf = _load_conf()
        f = ttk.Frame(self.container); f.pack(pady=30)
        ttk.Label(f, text="Stage 1: Connect to Remote Device", style="Header.TLabel").pack(pady=(0,15))
        row1 = ttk.Frame(f); row1.pack(pady=4, fill="x")
        ttk.Label(row1, text="  API Key:", width=13).pack(side="left")
        self.inp_apikey = ttk.Entry(row1, width=55, show="*"); self.inp_apikey.pack(side="left", padx=4)
        self.inp_apikey.insert(0, conf.get("api_key", ""))
        row2 = ttk.Frame(f); row2.pack(pady=4, fill="x")
        ttk.Label(row2, text="Ollama URL:", width=13).pack(side="left")
        self.inp_ollama = ttk.Entry(row2, width=55); self.inp_ollama.pack(side="left", padx=4)
        self.inp_ollama.insert(0, conf.get("ollama_url", ""))
        ttk.Label(f, text="From platform 'API调用' button, e.g. https://xxx.gdriscv.com",
                  style="Hint.TLabel").pack()
        ttk.Button(f, text="Connect", command=self._on_connect).pack(pady=12)
        self.conn_status = ttk.Label(f, text="", style="Status.TLabel", wraplength=700)
        self.conn_status.pack()
        ttk.Label(f, text="First run? Paste this in gdriscv.com web terminal to start agent:",
                  style="Hint.TLabel").pack(pady=(20,2))
        hint_box = tk.Text(f, height=3, width=90, font=("Consolas", 9), bg="#252526", fg="#6a9955",
                           relief="flat", wrap="word")
        hint_box.insert("1.0", AGENT_INSTALL_CMD)
        hint_box.config(state="disabled"); hint_box.pack()

    def _set_status(self, msg):
        self.conn_status.config(text=msg)

    def _on_connect(self):
        key = self.inp_apikey.get().strip()
        ollama = self.inp_ollama.get().strip()
        if not key or not ollama:
            self.conn_status.config(text="Please fill both fields."); return
        self.conn_status.config(text="Connecting...")
        _save_conf({"api_key": key, "ollama_url": ollama})
        self.api = RemoteAPI(key, ollama)
        def do():
            try:
                _dbg(f"connecting to {ollama}")
                r = self.api.health()
                _dbg(f"health: {r}")
                if r.get("ok"):
                    m = f"Connected! cwd={r.get('cwd','?')} version={r.get('version','?')}"
                    self._after(self._set_status, m)
                    self.root.after(500, self.build_stage2)
                else:
                    self._after(self._set_status, f"Failed: {r.get('error', r)}")
            except Exception:
                tb = traceback.format_exc(); _dbg(tb)
                self._after(self._set_status, tb.strip().split("\n")[-1][:300])
        self._bg(do)

    # ── Stage 2: Init environment ──
    def build_stage2(self):
        self._clear()
        ttk.Label(self.container, text="Stage 2: Initialize Remote Environment", style="Header.TLabel").pack(pady=(0,8))
        pwd_frame = ttk.Frame(self.container); pwd_frame.pack(fill="x", pady=4)
        ttk.Label(pwd_frame, text="sudo password:").pack(side="left")
        self.inp_sudopwd = ttk.Entry(pwd_frame, width=30, show="*"); self.inp_sudopwd.pack(side="left", padx=6)
        self.inp_sudopwd.insert(0, "bianbu")
        self.btn_start_init = ttk.Button(pwd_frame, text="Start Init", command=self._start_init)
        self.btn_start_init.pack(side="left", padx=6)
        self.btn_skip_init = ttk.Button(pwd_frame, text="Skip to Config", command=self.build_stage3)
        self.btn_skip_init.pack(side="left")
        self.init_log = scrolledtext.ScrolledText(self.container, width=105, height=20, font=("Consolas", 10),
                                                   bg="#0c0c0c", fg="#cccccc", insertbackground="#ccc", state="disabled")
        self.init_log.pack(fill="both", expand=True, pady=4)
        self.btn_cont2 = ttk.Button(self.container, text="Continue to Config", command=self.build_stage3)

    def _start_init(self):
        self.btn_start_init.config(state="disabled")
        self._sudo_pwd = self.inp_sudopwd.get()
        self._bg(self._run_init)

    def _log(self, msg):
        def do():
            self.init_log.config(state="normal")
            self.init_log.insert("end", msg + "\n")
            self.init_log.see("end")
            self.init_log.config(state="disabled")
        self._after(do)

    def _exec_log(self, cmd, timeout_sec=120):
        self._log(f"$ {cmd}")
        r = self.api.exec(cmd, timeout_sec=timeout_sec)
        out = r.get("stdout", "").strip()
        err = r.get("stderr", "").strip()
        if out: self._log(out)
        if err: self._log(f"[stderr] {err}")
        return r

    def _async_exec_wait(self, cmd, label="", poll_interval=3):
        self._log(f"$ {cmd}")
        r = self.api.async_exec(cmd)
        if not r.get("ok"):
            self._log(f"[ERROR] async_exec failed: {r.get('error', r)}"); return r
        job_id = r["job_id"]
        self._log(f"[job {job_id}] started{' — ' + label if label else ''}")
        last_len = 0
        while True:
            time.sleep(poll_interval)
            s = self.api.async_status(job_id, tail_lines=80)
            lines = s.get("stdout_tail", "").splitlines()
            if len(lines) > last_len:
                for line in lines[last_len:]: self._log(line)
                last_len = len(lines)
            if s.get("status") == "done":
                err = s.get("stderr_tail", "").strip()
                if err: self._log(f"[stderr] {err}")
                self._log(f"[job {job_id}] done (exit_code={s.get('exit_code',-1)})")
                return s

    def _sudo_cmd(self, cmd):
        pwd = self._sudo_pwd
        return f'echo {json.dumps(pwd)} | sudo -S bash -c {json.dumps(cmd)}' if pwd else f'sudo -n bash -c {json.dumps(cmd)}'

    def _run_init(self):
        try:
            self._log("=== Installing tmux ===")
            self._async_exec_wait(self._sudo_cmd("apt-get update -qq && apt-get install -y tmux"), label="apt install tmux")

            # Node.js 22 for riscv64 from unofficial-builds
            self._log("\n=== Installing Node.js 22 (riscv64 unofficial-builds) ===")
            r = self._exec_log("node --version 2>/dev/null || echo NONE")
            node_ver = r.get("stdout", "").strip()
            need_node = "NONE" in node_ver or (node_ver.startswith("v") and int(node_ver.split(".")[0][1:]) < 20)
            if need_node:
                self._async_exec_wait(
                    "cd /tmp && "
                    "curl -fsSL -L https://github.com/gounthar/unofficial-builds/releases/download/v22.22.0/node-v22.22.0-linux-riscv64.tar.xz -o node22.tar.xz && "
                    "tar xf node22.tar.xz && "
                    "sudo cp -r node-v22.22.0-linux-riscv64/{bin,lib,include,share} /usr/local/ && "
                    "rm -rf node22.tar.xz node-v22.22.0-linux-riscv64",
                    label="install node 22 riscv64")
                self._exec_log("node --version && npm --version")
            else:
                self._log(f"Node.js {node_ver} already >= 20, skipping")

            self._log("\n=== Setting npm mirror ===")
            self._exec_log("npm config set registry https://registry.npmmirror.com")

            self._log("\n=== Installing Claude CLI ===")
            r = self._exec_log("which claude 2>/dev/null || echo NOT_FOUND")
            if "NOT_FOUND" in r.get("stdout", ""):
                self._async_exec_wait(self._sudo_cmd("npm install -g @anthropic-ai/claude-code"), label="npm install claude-code")

            self._log("\n=== Installing Codex CLI ===")
            r = self._exec_log("which codex 2>/dev/null || echo NOT_FOUND")
            if "NOT_FOUND" in r.get("stdout", ""):
                self._async_exec_wait(self._sudo_cmd("npm install -g @openai/codex"), label="npm install codex")

            self._log("\n=== Initialization complete ===")
        except Exception as e:
            self._log(f"\n[ERROR] {e}"); _dbg(traceback.format_exc())
        self._after(lambda: self.btn_cont2.pack(pady=8))

    # ── Stage 3: Configure API keys ──
    def build_stage3(self):
        self._clear()
        f = ttk.Frame(self.container); f.pack(pady=30)
        ttk.Label(f, text="Stage 3: Configure CLI API Keys & URLs", style="Header.TLabel").pack(pady=(0,15))
        fields = [("Anthropic Key:", "*", "anthkey"), ("Anthropic URL:", "", "anthurl"),
                  ("OpenAI Key:", "*", "oaikey"), ("OpenAI URL:", "", "oaiurl")]
        for label, show, attr in fields:
            row = ttk.Frame(f); row.pack(pady=3, fill="x")
            ttk.Label(row, text=label, width=16).pack(side="left")
            e = ttk.Entry(row, width=52, show=show or ""); e.pack(side="left", padx=4)
            setattr(self, f"inp_{attr}", e)
        ttk.Label(f, text="URL optional. e.g. https://api.example.com/v1", style="Hint.TLabel").pack()
        ttk.Button(f, text="Save & Continue", command=self._on_save_keys).pack(pady=12)
        self.key_status = ttk.Label(f, text="", style="Status.TLabel"); self.key_status.pack()

    def _on_save_keys(self):
        akey = self.inp_anthkey.get().strip()
        aurl = self.inp_anthurl.get().strip()
        okey = self.inp_oaikey.get().strip()
        ourl = self.inp_oaiurl.get().strip()
        if not akey and not okey:
            self.key_status.config(text="Enter at least one key."); return
        self.key_status.config(text="Saving...")
        def do():
            try:
                lines = []
                if akey: lines.append(f'export ANTHROPIC_API_KEY="{akey}"')
                if aurl: lines.append(f'export ANTHROPIC_BASE_URL="{aurl}"')
                if okey: lines.append(f'export OPENAI_API_KEY="{okey}"')
                if ourl: lines.append(f'export OPENAI_BASE_URL="{ourl}"')
                self._bashrc_exports = "\n".join(lines)
                # Remove old entries and append new
                self.api.exec("sed -i '/ANTHROPIC_API_KEY\\|ANTHROPIC_BASE_URL\\|OPENAI_API_KEY\\|OPENAI_BASE_URL/d' ~/.bashrc")
                self.api.exec(f"cat >> ~/.bashrc << 'GDRISCV_EOF'\n{self._bashrc_exports}\nGDRISCV_EOF")
                self._after(self._set_status_key, "Keys saved!")
                self.root.after(500, self.build_stage4)
            except Exception as e:
                self._after(self._set_status_key, f"Error: {e}")
        self._bg(do)

    def _set_status_key(self, msg):
        self.key_status.config(text=msg)

    # ── Stage 4: Terminal tabs ──
    def build_stage4(self):
        self._clear()
        ttk.Label(self.container, text="Stage 4: Remote Terminals", style="Header.TLabel").pack(pady=(0,4))
        nb = ttk.Notebook(self.container)
        nb.pack(fill="both", expand=True, pady=4)
        self.term_widgets = {}
        tabs = [("claude", "Claude CLI", "source ~/.bashrc && claude"),
                ("codex", "Codex CLI", "source ~/.bashrc && codex"),
                ("shell", "Shell", "")]
        for name, label, start_cmd in tabs:
            frame = ttk.Frame(nb)
            nb.add(frame, text=f"  {label}  ")
            txt = scrolledtext.ScrolledText(frame, font=("Consolas", 10), bg="#0c0c0c", fg="#cccccc",
                                            insertbackground="#ccc", state="disabled", wrap="none")
            txt.pack(fill="both", expand=True)
            inp_frame = ttk.Frame(frame); inp_frame.pack(fill="x", pady=(3,0))
            ttk.Label(inp_frame, text=">", font=("Consolas", 12, "bold"), foreground="#569cd6").pack(side="left")
            inp = tk.Entry(inp_frame, font=("Consolas", 11), bg="#1a1a1a", fg="#e0e0e0",
                           insertbackground="#e0e0e0", relief="flat", highlightthickness=1,
                           highlightcolor="#569cd6", highlightbackground="#3c3c3c")
            inp.pack(side="left", fill="x", expand=True, padx=(4,0))
            inp.bind("<Return>", lambda e, n=name: self._on_term_send(n))
            self.term_widgets[name] = (txt, inp)
        self._bg(self._init_sessions, tabs)

    def _on_term_send(self, name):
        _, inp_w = self.term_widgets[name]
        text = inp_w.get().strip()
        if not text: return
        inp_w.delete(0, "end")
        self._bg(self._send_to_session, name, text)

    def _send_to_session(self, name, text):
        try: self.sessions[name].send_keys(text)
        except Exception as e: _dbg(f"send_keys error: {e}")

    def _init_sessions(self, tabs):
        for name, _, start_cmd in tabs:
            sess = TmuxSession(self.api, f"gdr_{name}")
            sess.create()
            self.sessions[name] = sess
            if start_cmd: sess.send_keys(start_cmd)
            self.poll_running[name] = True
            self._bg(self._poll_loop, name)

    def _poll_loop(self, name):
        last = ""
        while self.poll_running.get(name):
            try:
                raw = self.sessions[name].capture()
                cleaned = strip_ansi(raw).rstrip()
                if cleaned != last:
                    last = cleaned
                    self._after(self._update_term, name, cleaned)
            except Exception: pass
            time.sleep(0.8)

    def _update_term(self, name, text):
        txt_w, _ = self.term_widgets[name]
        txt_w.config(state="normal")
        txt_w.delete("1.0", "end")
        txt_w.insert("1.0", text)
        txt_w.see("end")
        txt_w.config(state="disabled")

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        for n in list(self.poll_running): self.poll_running[n] = False
        for s in self.sessions.values():
            try: s.kill()
            except: pass
        self.root.destroy()

if __name__ == "__main__":
    GdriscvGUI().run()
