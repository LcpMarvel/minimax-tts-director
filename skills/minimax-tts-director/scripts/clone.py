#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""MiniMax 声音复刻：本地网页录音/上传素材 → 上传文件 → 复刻音色 → 写入本地音色库。
serve 启动只监听 127.0.0.1 的网页；list 查看本地音色库（--account 额外查询账号内的复刻音色）。
密钥只从运行时 MINIMAX_API_KEY 读取，只在本进程内使用，不发给浏览器、不写盘、不打印。"""
import argparse
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOSTS = {"cn": "https://api.minimax.cn", "bj": "https://api-bj.minimaxi.com"}
KEY_ENV = "MINIMAX_API_KEY"
PAGE = Path(__file__).with_name("clone.html")
AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav"}
MAX_BYTES = 20 * 1024 * 1024
SOURCE_SECONDS = (10, 300)
PROMPT_SECONDS = 8
PREVIEW_LIMIT = 1000
MODELS = {"speech-2.8-hd": 3.5, "speech-2.8-turbo": 2.0, "speech-2.6-hd": 3.5, "speech-2.6-turbo": 2.0}
LANGUAGE_BOOST = {"Chinese", "Chinese,Yue", "English", "Arabic", "Russian", "Spanish", "French", "Portuguese",
                  "German", "Turkish", "Dutch", "Ukrainian", "Vietnamese", "Indonesian", "Japanese", "Italian",
                  "Korean", "Thai", "Polish", "Romanian", "Greek", "Czech", "Finnish", "Hindi", "Bulgarian",
                  "Danish", "Hebrew", "Malay", "Persian", "Slovak", "Swedish", "Croatian", "Filipino",
                  "Hungarian", "Norwegian", "Slovenian", "Catalan", "Nynorsk", "Tamil", "Afrikaans", "auto"}
VOICE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]")
CLONE_FEE = 9.9
IDLE_SECONDS = 180      # 页面关闭（心跳中断）后多久退出
STARTUP_SECONDS = 900   # 启动后一直没人打开页面时多久退出
UNUSED_DAYS = 7
HINTS = {
    1043: "试听文本与音频的相似度低于阈值",
    2037: "音频时长不符合要求：复刻素材 10 秒～5 分钟，示例音频小于 8 秒",
    2038: "账号没有复刻权限：需要先在 MiniMax 开放平台完成个人或企业实名认证",
    2039: "voice_id 已存在（包括已删除的），请换一个",
    2048: "示例音频超过 8 秒",
    2051: "素材质量检测未通过，请换更清晰、噪声更少的录音",
    2052: "账号的复刻音色数量已达上限",
}
HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U000323af]")


class ApiError(RuntimeError):
    def __init__(self, message, code=None, trace=None):
        super().__init__(message)
        self.code, self.trace = code, trace


class Uncertain(RuntimeError):
    """请求可能已到达服务端，结果未知。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def billed_characters(text):
    return sum(2 if HAN.fullmatch(ch) else 1 for ch in text)


def check_voice_id(voice_id):
    if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
        raise ValueError("voice_id 须为 8～256 位，字母开头，只含字母、数字、- 和 _，且不能以 - 或 _ 结尾")


def wav_seconds(data):
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"WAV 文件无法解析：{exc}") from exc


def multipart(fields, filename, data, content_type):
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
               f"Content-Type: {content_type}\r\n\r\n".encode())
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


class Api:
    def __init__(self, host, key, timeout=120):
        if not key:
            raise ValueError(f"缺少 {KEY_ENV}")
        self.host, self.key, self.timeout = host, key, timeout

    def post(self, path, data, content_type):
        request = urllib.request.Request(self.host + path, data=data, method="POST", headers={
            "Content-Type": content_type, "Authorization": "Bearer " + self.key})
        try:
            # 不跟随重定向，避免凭据转发到其他目的地。
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                trace, payload = response.headers.get("Trace-Id"), json.load(response)
        except urllib.error.HTTPError as exc:
            raise ApiError(f"HTTP {exc.code}：{exc.read(2000).decode('utf-8', 'replace').replace(self.key, '[REDACTED]')}",
                           trace=exc.headers.get("Trace-Id")) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise Uncertain(f"{path} 网络异常或响应无法解析：{type(exc).__name__}") from None
        base = payload.get("base_resp")
        if not isinstance(base, dict) or not isinstance(base.get("status_code"), int):
            raise ApiError(f"{path} 响应缺少 base_resp", trace=trace)
        code = base["status_code"]
        if code != 0:
            message = f"错误码 {code}：{base.get('status_msg', '')}"
            if code in HINTS:
                message += f"（{HINTS[code]}）"
            raise ApiError(message, code=code, trace=trace)
        return payload, trace

    def json(self, path, body):
        return self.post(path, json.dumps(body, ensure_ascii=False).encode(), "application/json")

    def upload(self, data, ext, purpose):
        body, content_type = multipart({"purpose": purpose}, f"{purpose}.{ext}", data, AUDIO_TYPES[ext])
        payload, _ = self.post("/v1/files/upload", body, content_type)
        file_id = (payload.get("file") or {}).get("file_id")
        if not isinstance(file_id, int):
            raise ApiError("上传响应缺少 file.file_id")
        return file_id

    def clone(self, body):
        return self.json("/v1/voice_clone", body)

    def account_voices(self):
        payload, _ = self.json("/v1/get_voice", {"voice_type": "voice_cloning"})
        return payload.get("voice_cloning") or []

    def delete(self, voice_id):
        self.json("/v1/delete_voice", {"voice_type": "voice_cloning", "voice_id": voice_id})


def download(url, timeout=60):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("试听音频链接须为 https")
    # 预签名对象存储链接，不携带 API 密钥。
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
        content_type = response.headers.get_content_type()
    if len(data) > MAX_BYTES:
        raise ValueError("试听音频过大")
    ext = Path(parsed.path).suffix.lstrip(".").lower() or (mimetypes.guess_extension(content_type) or ".mp3")[1:]
    if ext not in {"mp3", "wav", "m4a", "flac", "pcm"}:
        raise ValueError(f"试听音频格式未知：{ext}")
    return data, ext


class Library:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.dir = self.path.parent / (self.path.stem + "-audio")
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
                raise ValueError(f"音色库格式无效：{self.path}")
            self.data = data
        else:
            self.data = {"voices": []}

    def find(self, voice_id):
        return next((v for v in self.data["voices"] if v["voice_id"] == voice_id), None)

    def add(self, entry):
        if self.find(entry["voice_id"]):
            raise ValueError(f"音色库里已有 {entry['voice_id']}")
        self.data["voices"].append(entry)
        save(self.path, self.data)

    def update(self, voice_id, **fields):
        entry = self.find(voice_id)
        if not entry:
            raise ValueError(f"音色库里没有 {voice_id}")
        entry.update(fields)
        save(self.path, self.data)
        return entry

    def store(self, name, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_bytes(data)
        return f"{self.dir.name}/{name}"

    def read(self, relative):
        target = (self.path.parent / relative).resolve()
        if target.parent != self.dir.resolve() or not target.is_file():
            raise ValueError("文件不存在")
        return target.read_bytes()


class Session:
    """网页会话：暂存上传的素材，执行复刻并写入音色库。"""

    def __init__(self, library, api):
        self.library, self.api = library, api
        self.pending = {}
        self.cloned = []
        self.lock = threading.Lock()

    def receive(self, kind, ext, data):
        if kind not in {"source", "prompt"}:
            raise ValueError("kind 须为 source 或 prompt")
        if ext not in AUDIO_TYPES:
            raise ValueError("只支持 mp3、m4a、wav")
        if not data or len(data) > MAX_BYTES:
            raise ValueError("音频须为 1 字节～20MB")
        seconds = wav_seconds(data) if ext == "wav" else None
        if seconds is not None:
            if kind == "source" and not SOURCE_SECONDS[0] <= seconds <= SOURCE_SECONDS[1]:
                raise ValueError(f"复刻素材时长 {seconds:.1f} 秒，须在 10 秒～5 分钟之间")
            if kind == "prompt" and seconds >= PROMPT_SECONDS:
                raise ValueError(f"示例音频时长 {seconds:.1f} 秒，须小于 8 秒")
        audio_id = uuid.uuid4().hex
        self.pending[audio_id] = {"kind": kind, "ext": ext, "data": data, "seconds": seconds}
        return {"id": audio_id, "bytes": len(data), "seconds": seconds}

    def take(self, audio_id, kind):
        item = self.pending.get(audio_id)
        if not item or item["kind"] != kind:
            raise ValueError(f"找不到已上传的{'复刻素材' if kind == 'source' else '示例音频'}，请重新选择")
        return item

    def check(self, form):
        if form.get("consent") is not True:
            raise ValueError("需要确认拥有该声音的使用授权")
        voice_id = form.get("voice_id")
        check_voice_id(voice_id)
        if self.library.find(voice_id):
            raise ValueError(f"音色库里已有 {voice_id}，请换一个 voice_id")
        label = form.get("label", "")
        if not isinstance(label, str) or len(label) > 50:
            raise ValueError("显示名须为 50 字以内的字符串")
        source = self.take(form.get("source_id"), "source")
        prompt = None
        if form.get("prompt_id"):
            prompt = self.take(form["prompt_id"], "prompt")
            text = form.get("prompt_text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("使用示例音频时必须填写它的逐字文本")
            if not re.search(r"[。！？!?.，,；;…]$", text.strip()):
                raise ValueError("示例音频文本须以标点结尾")
        elif form.get("prompt_text"):
            raise ValueError("填写了示例文本但没有示例音频")
        preview = form.get("preview_text", "")
        if not isinstance(preview, str) or len(preview) > PREVIEW_LIMIT:
            raise ValueError(f"试听文本须为 {PREVIEW_LIMIT} 字以内")
        model = form.get("model")
        if preview.strip() and model not in MODELS:
            raise ValueError("试听需要选择模型：" + "、".join(MODELS))
        boost = form.get("language_boost")
        if boost not in (None, "") and boost not in LANGUAGE_BOOST:
            raise ValueError(f"language_boost 不支持：{boost}")
        for flag in ("noise_reduction", "volume_normalization"):
            if not isinstance(form.get(flag, False), bool):
                raise ValueError(f"{flag} 须为布尔值")
        return voice_id, label.strip(), source, prompt, preview.strip(), model, boost or None

    def clone(self, form):
        with self.lock:
            voice_id, label, source, prompt, preview, model, boost = self.check(form)
            created = now()
            entry = {"voice_id": voice_id, "label": label, "status": "uncertain", "created_at": created.isoformat(),
                     "delete_if_unused_after": (created + timedelta(days=UNUSED_DAYS)).isoformat(),
                     "source": {"audio": self.library.store(f"{voice_id}.source.{source['ext']}", source["data"]),
                                "bytes": len(source["data"]), "seconds": source["seconds"]},
                     "options": {"noise_reduction": form.get("noise_reduction", False),
                                 "volume_normalization": form.get("volume_normalization", False),
                                 "language_boost": boost}}
            if prompt:
                entry["prompt"] = {"audio": self.library.store(f"{voice_id}.prompt.{prompt['ext']}", prompt["data"]),
                                   "text": form["prompt_text"].strip()}
            # 上传文件不产生复刻；在此之前失败不写入音色库。
            source_file = self.api.upload(source["data"], source["ext"], "voice_clone")
            body = {"file_id": source_file, "voice_id": voice_id,
                    "need_noise_reduction": entry["options"]["noise_reduction"],
                    "need_volume_normalization": entry["options"]["volume_normalization"]}
            if prompt:
                body["clone_prompt"] = {"prompt_audio": self.api.upload(prompt["data"], prompt["ext"], "prompt_audio"),
                                        "prompt_text": entry["prompt"]["text"]}
            if preview:
                body.update(text=preview, model=model)
                entry["preview"] = {"text": preview, "model": model, "billed_characters": billed_characters(preview)}
            if boost:
                body["language_boost"] = boost
            entry["file_id"] = source_file
            try:
                payload, trace = self.api.clone(body)
            except Uncertain as exc:
                entry["error"] = str(exc)
                self.library.add(entry)
                raise Uncertain(f"{exc}。复刻结果未知，已在音色库记为 uncertain；请先到 MiniMax 控制台核对，"
                                "不要换个 voice_id 盲目重试") from None
            entry.update(status="cloned", trace_id=trace)
            if payload.get("input_sensitive"):
                entry["input_sensitive"] = payload.get("input_sensitive_type")
            demo = payload.get("demo_audio")
            if preview:
                if not demo:
                    entry["preview"]["error"] = "响应没有 demo_audio"
                else:
                    try:
                        data, ext = download(demo)
                        entry["preview"]["audio"] = self.library.store(f"{voice_id}.preview.{ext}", data)
                    except (ValueError, OSError) as exc:
                        entry["preview"]["error"] = f"试听音频下载失败：{exc}"
            self.library.add(entry)
            self.cloned.append(voice_id)
            for item in (source, prompt):
                if item:
                    self.pending = {k: v for k, v in self.pending.items() if v is not item}
            return entry

    def delete(self, voice_id, confirm):
        with self.lock:
            check_voice_id(voice_id)
            if confirm != voice_id:
                raise ValueError("删除需要再次输入完全一致的 voice_id")
            self.api.delete(voice_id)
            if self.library.find(voice_id):
                self.library.update(voice_id, status="deleted", deleted_at=now().isoformat())
            return {"voice_id": voice_id, "deleted": True}


class Lifecycle:
    """结束条件：用户点按钮、页面关闭（心跳中断）、或一直没人打开。请求处理中不会因空闲退出。"""

    def __init__(self, idle, startup):
        self.idle, self.startup = idle, startup
        self.finished, self.lock = threading.Event(), threading.Lock()
        self.reason, self.opened, self.active, self.last_seen = None, False, 0, time.monotonic()

    def end(self, reason):
        with self.lock:
            if not self.reason:
                self.reason = reason
        self.finished.set()

    def enter(self):
        with self.lock:
            self.active += 1

    def leave(self, authorized):
        with self.lock:
            self.active -= 1
            if authorized:
                self.opened, self.last_seen = True, time.monotonic()

    def check(self):
        with self.lock:
            if self.active:
                return
            quiet = time.monotonic() - self.last_seen
            reason = ("page_closed" if self.opened and quiet > self.idle else
                      "never_opened" if not self.opened and quiet > self.startup else None)
        if reason:
            self.end(reason)


def make_handler(session, token, port_holder, life):
    page = PAGE.read_text(encoding="utf-8")
    settings = {"preview_limit": PREVIEW_LIMIT, "models": MODELS, "clone_fee": CLONE_FEE, "unused_days": UNUSED_DAYS,
                "max_bytes": MAX_BYTES, "source_seconds": SOURCE_SECONDS, "prompt_seconds": PROMPT_SECONDS,
                "language_boost": sorted(LANGUAGE_BOOST), "library": str(session.library.path)}

    class Handler(BaseHTTPRequestHandler):
        server_version = "minimax-clone"

        def log_message(self, fmt, *args):
            pass

        def handle_one_request(self):
            self.authorized = False
            life.enter()
            try:
                super().handle_one_request()
            finally:
                life.leave(self.authorized)

        def reply(self, status, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            port = port_holder[0]
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.reply(403, {"error": "Host 不允许"})
                return False
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            given = self.headers.get("X-Clone-Token") or (query.get("token") or [""])[0]
            if not hmac.compare_digest(given.encode(), token.encode()):
                self.reply(403, {"error": "token 无效"})
                return False
            self.authorized = True
            return True

        def body(self, limit):
            length = self.headers.get("Content-Length")
            if not length or not length.isdigit() or int(length) > limit:
                raise ValueError("请求体缺失或过大")
            return self.rfile.read(int(length))

        def do_GET(self):
            if not self.allowed():
                return
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/":
                    self.reply(200, page.encode(), "text/html; charset=utf-8")
                elif path == "/api/ping":
                    self.reply(200, {"ok": True})
                elif path == "/api/state":
                    self.reply(200, {"settings": settings, "voices": session.library.data["voices"],
                                     "cloned": session.cloned})
                elif path == "/api/audio":
                    relative = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("path", [""])[0]
                    data = session.library.read(relative)
                    self.reply(200, data, mimetypes.guess_type(relative)[0] or "application/octet-stream")
                else:
                    self.reply(404, {"error": "not found"})
            except ValueError as exc:
                self.reply(404, {"error": str(exc)})

        def do_POST(self):
            if not self.allowed():
                return
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path == "/api/upload":
                    result = session.receive(query.get("kind", [""])[0], query.get("ext", [""])[0].lower(),
                                             self.body(MAX_BYTES))
                elif parsed.path == "/api/finish":
                    result = {"finished": True}
                    life.end("finished")
                else:
                    form = json.loads(self.body(64 * 1024) or b"{}")
                    if not isinstance(form, dict):
                        raise ValueError("请求须为 JSON 对象")
                    if parsed.path == "/api/clone":
                        result = session.clone(form)
                    elif parsed.path == "/api/account":
                        result = {"voices": session.api.account_voices()}
                    elif parsed.path == "/api/delete":
                        result = session.delete(form.get("voice_id"), form.get("confirm"))
                    else:
                        self.reply(404, {"error": "not found"})
                        return
                self.reply(200, result)
            except Uncertain as exc:
                self.reply(502, {"error": str(exc), "uncertain": True})
            except ApiError as exc:
                self.reply(502, {"error": str(exc), "code": exc.code, "trace_id": exc.trace})
            except (ValueError, json.JSONDecodeError) as exc:
                self.reply(400, {"error": str(exc)})

    return Handler


def serve(library, api, port=0, open_browser=False, announce=print, idle=IDLE_SECONDS, startup=STARTUP_SECONDS,
          poll=5.0):
    session = Session(library, api)
    token = secrets.token_urlsafe(24)
    port_holder, life = [port], Lifecycle(idle, startup)
    server = HTTPServer(("127.0.0.1", port), make_handler(session, token, port_holder, life))
    port_holder[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_holder[0]}/?token={token}"
    announce(json.dumps({"url": url, "library": str(library.path)}, ensure_ascii=False))
    if open_browser:
        webbrowser.open(url)

    def stop_when_finished():
        while not life.finished.wait(poll):
            life.check()
        server.shutdown()

    threading.Thread(target=stop_when_finished, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return {"library": str(library.path), "ended": life.reason, "cloned": session.cloned,
            "voices": [{"voice_id": v["voice_id"], "label": v["label"], "status": v["status"]}
                       for v in library.data["voices"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve")
    run.add_argument("--library", type=Path, required=True, help="本地音色库 JSON；素材与试听音频存到同名 -audio 目录")
    run.add_argument("--port", type=int, default=0, help="默认随机空闲端口")
    run.add_argument("--region", choices=sorted(HOSTS), default="cn")
    run.add_argument("--open", action="store_true", help="启动后用默认浏览器打开")
    run.add_argument("--idle-timeout", type=float, default=IDLE_SECONDS, help="页面关闭后多少秒自动退出")
    run.add_argument("--startup-timeout", type=float, default=STARTUP_SECONDS, help="一直没人打开页面时多少秒后退出")
    show = sub.add_parser("list")
    show.add_argument("--library", type=Path, required=True)
    show.add_argument("--account", action="store_true", help="同时查询账号内的复刻音色（需密钥，只读）")
    show.add_argument("--region", choices=sorted(HOSTS), default="cn")
    args = parser.parse_args()
    try:
        library = Library(args.library)
        if args.command == "serve":
            result = serve(library, Api(HOSTS[args.region], os.environ.get(KEY_ENV)), args.port, args.open,
                           announce=lambda line: print(line, flush=True), idle=args.idle_timeout,
                           startup=args.startup_timeout)
        else:
            result = {"library": str(library.path), "voices": library.data["voices"]}
            if args.account:
                result["account"] = Api(HOSTS[args.region], os.environ.get(KEY_ENV)).account_voices()
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
