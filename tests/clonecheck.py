"""声音复刻离线自检：python3 tests/clonecheck.py（只用标准库，不联网、不读取密钥）。"""
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/minimax-tts-director/scripts"))
import clone  # noqa: E402


def wav(seconds, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(seconds * rate))
    return buf.getvalue()


class FakeApi:
    def __init__(self, clone_error=None):
        self.calls, self.clone_error = [], clone_error

    def upload(self, data, ext, purpose):
        self.calls.append(("upload", purpose, ext, len(data)))
        return 1000 + len(self.calls)

    def clone(self, body):
        self.calls.append(("clone", body))
        if self.clone_error:
            raise self.clone_error
        return {"demo_audio": "https://example.invalid/demo/abc.mp3" if body.get("text") else "",
                "input_sensitive": False, "base_resp": {"status_code": 0}}, "trace-1"

    def account_voices(self):
        self.calls.append(("account",))
        return [{"voice_id": "Existing01", "description": [], "created_time": "2026-11-01"}]

    def delete(self, voice_id):
        self.calls.append(("delete", voice_id))


class Server:
    """在线程里跑 serve，返回可访问的 URL。"""

    def __init__(self, library_path, api, **options):
        self.library, self.api, self.lines = clone.Library(library_path), api, []
        ready = threading.Event()

        def announce(line):
            self.lines.append(json.loads(line))
            ready.set()

        self.result = None
        self.thread = threading.Thread(target=lambda: setattr(self, "result", clone.serve(self.library, api, 0, False, announce, **options)))
        self.thread.start()
        assert ready.wait(5)
        self.url = self.lines[0]["url"]
        self.base, self.token = self.url.split("/?token=")

    def request(self, path, data=None, headers=None, token=True, content_type="application/json"):
        hdrs = {"X-Clone-Token": self.token} if token else {}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
        if data is not None:
            hdrs["Content-Type"] = content_type
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, path, data=None, **kwargs):
        status, body = self.request(path, data, **kwargs)
        return status, json.loads(body)

    def finish(self):
        self.request("/api/finish", b"")
        self.thread.join(5)
        assert not self.thread.is_alive()


class Clone(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "voices" / "clones.json"
        patcher = mock.patch.object(clone, "download", return_value=(b"ID3fake", "mp3"))
        self.download = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def form(self, source_id, **extra):
        return {"voice_id": "MyVoice_2026", "label": "我的声音", "consent": True, "source_id": source_id,
                "preview_text": "你好，世界。", "model": "speech-2.8-hd", "noise_reduction": True, **extra}

    def test_full_flow_writes_library_and_serves_audio(self):
        api = FakeApi()
        server = Server(self.path, api)
        try:
            status, page = server.request("/?token=" + server.token, token=False)
            self.assertEqual(status, 200)
            self.assertIn("复刻你的声音", page.decode())
            self.assertNotIn(server.token, page.decode())
            status, src = server.json("/api/upload?kind=source&ext=wav", wav(12), content_type="audio/wav")
            self.assertEqual((status, src["seconds"]), (200, 12))
            status, prompt = server.json("/api/upload?kind=prompt&ext=wav", wav(3), content_type="audio/wav")
            status, entry = server.json("/api/clone", self.form(src["id"], prompt_id=prompt["id"],
                                                                 prompt_text="今天天气不错。", language_boost="Chinese"))
            self.assertEqual(status, 200, entry)
            self.assertEqual(entry["status"], "cloned")
            self.assertEqual(entry["preview"]["billed_characters"], 10)
            self.assertEqual(entry["preview"]["audio"], "clones-audio/MyVoice_2026.preview.mp3")
            body = api.calls[-1][1]
            self.assertEqual(body, {"file_id": 1001, "voice_id": "MyVoice_2026", "need_noise_reduction": True,
                                    "need_volume_normalization": False,
                                    "clone_prompt": {"prompt_audio": 1002, "prompt_text": "今天天气不错。"},
                                    "text": "你好，世界。", "model": "speech-2.8-hd", "language_boost": "Chinese"})
            self.assertEqual([c[1] for c in api.calls[:2]], ["voice_clone", "prompt_audio"])
            saved = json.loads(self.path.read_text())
            self.assertEqual([v["voice_id"] for v in saved["voices"]], ["MyVoice_2026"])
            self.assertTrue((self.path.parent / "clones-audio/MyVoice_2026.source.wav").is_file())
            status, data = server.request("/api/audio?path=clones-audio/MyVoice_2026.preview.mp3")
            self.assertEqual((status, data), (200, b"ID3fake"))
            status, _ = server.request("/api/audio?path=clones.json")
            self.assertEqual(status, 404)
            # 素材用过后不能再次引用；同名 voice_id 被拒绝。
            status, err = server.json("/api/clone", self.form(src["id"]))
            self.assertEqual(status, 400)
            status, src2 = server.json("/api/upload?kind=source&ext=wav", wav(12), content_type="audio/wav")
            status, err = server.json("/api/clone", self.form(src2["id"]))
            self.assertIn("已有", err["error"])
        finally:
            server.finish()
        self.assertEqual(server.result["cloned"], ["MyVoice_2026"])

    def test_security_checks(self):
        server = Server(self.path, FakeApi())
        try:
            self.assertEqual(server.request("/api/state", token=False)[0], 403)
            self.assertEqual(server.request("/api/state", headers={"X-Clone-Token": "wrong"})[0], 403)
            self.assertEqual(server.request("/api/state", headers={"Host": "evil.example"})[0], 403)
            self.assertEqual(server.request("/api/state")[0], 200)
        finally:
            server.finish()

    def test_finish_reason(self):
        server = Server(self.path, FakeApi())
        server.finish()
        self.assertEqual(server.result["ended"], "finished")

    def test_page_closed_and_never_opened_shut_down(self):
        server = Server(self.path, FakeApi(), idle=0.3, startup=60, poll=0.05)
        self.assertEqual(server.request("/api/ping")[0], 200)
        server.thread.join(3)
        self.assertFalse(server.thread.is_alive())
        self.assertEqual(server.result["ended"], "page_closed")
        # 未授权的请求不算“页面打开”，也不续命。
        server = Server(self.path, FakeApi(), idle=60, startup=0.3, poll=0.05)
        server.request("/api/ping", token=False)
        server.thread.join(3)
        self.assertEqual(server.result["ended"], "never_opened")

    def test_busy_request_blocks_idle_exit(self):
        life = clone.Lifecycle(idle=-1, startup=-1)
        life.enter()
        life.check()
        self.assertFalse(life.finished.is_set())
        life.leave(authorized=True)
        life.check()
        self.assertEqual(life.reason, "page_closed")

    def test_validation(self):
        session = clone.Session(clone.Library(self.path), FakeApi())
        with self.assertRaisesRegex(ValueError, "10 秒"):
            session.receive("source", "wav", wav(5))
        with self.assertRaisesRegex(ValueError, "小于 8 秒"):
            session.receive("prompt", "wav", wav(8))
        with self.assertRaisesRegex(ValueError, "mp3"):
            session.receive("source", "ogg", b"x")
        source = session.receive("source", "mp3", b"fake-mp3")["id"]
        for bad, pattern in ((dict(consent=False), "授权"), (dict(voice_id="short"), "8～256"),
                             (dict(voice_id="1abcdefgh"), "字母开头"), (dict(voice_id="abcdefgh_"), "结尾"),
                             (dict(model=None), "模型"), (dict(prompt_text="多余。"), "没有示例音频"),
                             (dict(language_boost="Klingon"), "language_boost"),
                             (dict(preview_text="字" * 1001), "1000")):
            with self.assertRaisesRegex(ValueError, pattern):
                session.check(self.form(source, **bad))
        prompt = session.receive("prompt", "wav", wav(2))["id"]
        with self.assertRaisesRegex(ValueError, "标点"):
            session.check(self.form(source, prompt_id=prompt, prompt_text="没有标点"))
        self.assertEqual(session.api.calls, [])
        self.assertFalse(self.path.exists())

    def test_api_error_is_not_saved_but_uncertain_is(self):
        session = clone.Session(clone.Library(self.path), FakeApi(clone.ApiError("错误码 2038", code=2038)))
        source = session.receive("source", "wav", wav(12))["id"]
        with self.assertRaises(clone.ApiError):
            session.clone(self.form(source))
        self.assertFalse(self.path.exists())
        session.api.clone_error = clone.Uncertain("timeout")
        with self.assertRaisesRegex(clone.Uncertain, "uncertain"):
            session.clone(self.form(source))
        entry = clone.Library(self.path).find("MyVoice_2026")
        self.assertEqual(entry["status"], "uncertain")
        self.assertEqual(session.cloned, [])

    def test_delete_requires_confirmation(self):
        session = clone.Session(clone.Library(self.path), FakeApi())
        source = session.receive("source", "wav", wav(12))["id"]
        session.clone(self.form(source, preview_text=""))
        self.assertNotIn("preview", session.library.find("MyVoice_2026"))
        self.download.assert_not_called()
        with self.assertRaisesRegex(ValueError, "再次输入"):
            session.delete("MyVoice_2026", "MyVoice")
        session.delete("MyVoice_2026", "MyVoice_2026")
        self.assertEqual(clone.Library(self.path).find("MyVoice_2026")["status"], "deleted")
        self.assertEqual(session.api.calls[-1], ("delete", "MyVoice_2026"))

    def test_api_parses_base_resp_and_redacts_key(self):
        api = clone.Api("https://api.example.invalid", "sk-secret")

        class Response(io.BytesIO):
            headers = {"Trace-Id": "t"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(payload):
            return mock.Mock(open=lambda req, timeout: Response(json.dumps(payload).encode()))

        with mock.patch.object(urllib.request, "build_opener", return_value=opener(
                {"base_resp": {"status_code": 2038, "status_msg": "no permission"}})):
            with self.assertRaisesRegex(clone.ApiError, "实名认证"):
                api.clone({})
        with mock.patch.object(urllib.request, "build_opener", return_value=opener(
                {"file": {"file_id": 42}, "base_resp": {"status_code": 0}})):
            self.assertEqual(api.upload(b"abc", "wav", "voice_clone"), 42)
        body, content_type = clone.multipart({"purpose": "voice_clone"}, "voice_clone.wav", b"RIFF", "audio/wav")
        self.assertIn(b'name="purpose"\r\n\r\nvoice_clone\r\n', body)
        self.assertIn(b'filename="voice_clone.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFF\r\n', body)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        with self.assertRaisesRegex(ValueError, "MINIMAX_API_KEY"):
            clone.Api("https://x", None)


if __name__ == "__main__":
    unittest.main(verbosity=1)
