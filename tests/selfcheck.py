"""离线自检：uv run --with pypinyin==0.55.0 python tests/selfcheck.py
字幕夹具来自 2026-11-09 两次真实 MiniMax 调用的 subtitle_file（不含链接或凭据）。"""
import io
import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/minimax-tts-director/scripts"))
import tts  # noqa: E402

FIXTURES = ROOT / "tests/fixtures"
TABLE = {"version": 1, "words": {"Anthropic": "ænˈθrɒpɪk"}, "acronyms": {"API": "A P I"},
         "models": ["5.5"], "decimal_point": "点"}


def reviewed(text, overrides=()):
    """把扫描候选全部标为已校对的自然读法，再用 overrides 替换指定位置。"""
    rows = {r["start"]: {**r, "status": "reviewed", "note": "测试"} for r in tts.scan(text)}
    for row in overrides:
        for j in range(len(row["text"])):
            rows.pop(row["start"] + j, None)
        rows[row["start"]] = {"status": "reviewed", **row}
    return sorted(rows.values(), key=lambda r: r["start"])


def probe1_plan():
    text = "华老栓说：这件华丽的衣服，用 API 处理。"
    return {"title": "探测一", "voices": {"旁白": "male-qn-jingying"}, "clips": [{
        "id": "p1", "text": text,
        "readings": reviewed(text, [{"start": 0, "text": "华", "pinyin": ["hua4"], "delivery": "inline"}]),
        "marks": [{"at": 5, "pause": 0.5}, {"at": 5, "tag": "laughs"}]}]}


def probe2_plan():
    text = "华老栓穿得很华丽。Anthropic 发布 Haiku 5.5，api 和 API。"
    return {"title": "探测二", "voices": {"旁白": "male-qn-jingying"}, "pronunciation_table": TABLE, "clips": [{
        "id": "p2", "text": text, "language_boost": "Chinese",
        "readings": reviewed(text, [
            {"start": 0, "text": "华老栓", "pinyin": ["hua4", "lao3", "shuan1"], "delivery": "inline"},
            {"start": 6, "text": "华丽", "pinyin": ["hua2", "li4"], "delivery": "dictionary"}])}]}


def wav_hex(seconds):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00" * int(24000 * seconds))
    return buffer.getvalue().hex()


class Fake:
    """模拟 MiniMax：按提交文本返回对应夹具的字幕。"""

    def __init__(self, subtitles=None, code=0, seconds=6.2):
        self.calls, self.subtitles, self.code, self.seconds = [], subtitles or {}, code, seconds

    def call(self, endpoint, body, key, timeout):
        self.calls.append(body)
        if self.code:
            return 200, "trace", {"base_resp": {"status_code": self.code, "status_msg": "voice id not exist"}}
        data = {"audio": wav_hex(self.seconds), "status": 2}
        if body["subtitle_enable"]:
            data["subtitle_file"] = "https://example.invalid/" + str(len(self.calls))
            self.pending = self.subtitles.get(body["text"], [])
        return 200, "trace", {"data": data, "extra_info": {"usage_characters": tts.billed_characters(body["text"]),
                                                           "audio_length": int(self.seconds * 1000)},
                              "base_resp": {"status_code": 0, "status_msg": "success"}}

    def download(self, url, timeout):
        return self.pending


class Probe(unittest.TestCase):
    def test_submitted_text_and_billing_match_real_calls(self):
        for plan, submitted, usage in (
                (probe1_plan(), "(hua4)老栓说：<#0.5#>(laughs)这件华丽的衣服，用 API 处理。", 55),
                (probe2_plan(), "(hua4)(lao3)(shuan1)穿得很华丽。Anthropic 发布 Haiku 5.5，api 和 API。", 67)):
            plan = tts.resolve_voices(plan)
            info = tts.validate(plan, complete=True)[0]
            self.assertEqual(info["submitted"], submitted)
            self.assertEqual(tts.billed_characters(info["submitted"]), usage)
        info = tts.validate(tts.resolve_voices(probe2_plan()), complete=True)[0]
        self.assertEqual(sorted(info["rules"]), sorted(["华丽/(hua2)(li4)", "Anthropic/(ænˈθrɒpɪk)", "API/A P I", "5.5/5点5"]))

    def test_subtitles_map_back_to_original(self):
        plan = tts.resolve_voices(probe1_plan())
        info = tts.validate(plan, complete=True)[0]
        raw = json.loads((FIXTURES / "probe1.subtitle.json").read_text(encoding="utf-8"))
        data = tts.parse_subtitles(raw, plan["clips"][0], info, 5.143)
        self.assertEqual(data["words"][0]["word"], "华")
        self.assertEqual(data["words"][0]["submitted"], "(hua4)")
        self.assertAlmostEqual(data["words"][0]["startTime"], 0.042667, places=5)
        self.assertAlmostEqual(data["words"][0]["endTime"], 0.213333, places=5)
        self.assertEqual([s["text"] for s in data["sentences"]], ["华老栓说：", "这件华丽的衣服，用 API 处理。"])
        self.assertEqual(data["tags"][0]["tag"], "laughs")
        self.assertTrue(all(plan["clips"][0]["text"][w["start"]:w["end"]] == w["word"] for w in data["words"]))
        plan = tts.resolve_voices(probe2_plan())
        info = tts.validate(plan, complete=True)[0]
        raw = json.loads((FIXTURES / "probe2.subtitle.json").read_text(encoding="utf-8"))
        data = tts.parse_subtitles(raw, plan["clips"][0], info, 6.141)
        self.assertEqual([w["word"] for w in data["words"][:3]], ["华", "老", "栓"])
        self.assertEqual(data["words"][0]["pronounce"], "ㄏㄨㄚˋ")
        flower = [w for w in data["words"] if w["word"] == "华丽"][0]
        self.assertEqual(flower["pronounce"], "ㄏㄨㄚˊㄌㄧˋ")
        self.assertNotIn("pronounce", [w for w in data["words"] if w["word"] == "穿"][0])
        self.assertEqual(data["sentences"][0]["text"], plan["clips"][0]["text"])

    def test_subtitle_position_mismatch_is_rejected(self):
        plan = tts.resolve_voices(probe1_plan())
        info = tts.validate(plan, complete=True)[0]
        raw = json.loads((FIXTURES / "probe1.subtitle.json").read_text(encoding="utf-8"))
        raw[0]["timestamped_words"][4]["word_begin"] += 1
        with self.assertRaises(ValueError):
            tts.parse_subtitles(raw, plan["clips"][0], info, 5.143)


class Validation(unittest.TestCase):
    def check(self, clip, plan_extra=None, complete=True):
        if "readings" not in clip and tts.HAN.search(clip["text"]) and complete:
            clip = {**clip, "readings": reviewed(clip["text"])}
        plan = {"voices": {"旁白": "male-qn-jingying"}, "clips": [clip], **(plan_extra or {})}
        return tts.validate(tts.resolve_voices(plan), complete=complete)

    def test_prepare_keeps_text_and_marks_pending(self):
        text = "银行的行长来了。" * 80
        plan = tts.prepare(text)
        self.assertEqual("".join(c["text"] for c in plan["clips"]), text)
        self.assertTrue(all(r["status"] == "pending" for c in plan["clips"] for r in c["readings"]))
        self.assertNotIn("pronunciation_table", plan)
        with self.assertRaises(ValueError):
            tts.validate(tts.resolve_voices(plan), complete=True)
        self.assertIn("pronunciation_table", tts.prepare("用 API 调 Claude。"))

    def test_original_text_must_stay_clean(self):
        for text in ("(hua4)老栓", "停一下<#1#>再说", "哈哈(laughs)"):
            with self.assertRaises(ValueError):
                self.check({"id": "a", "text": text}, complete=False)

    def test_delivery_is_required(self):
        with self.assertRaisesRegex(ValueError, "delivery"):
            self.check({"id": "a", "text": "行", "readings": [
                {"start": 0, "text": "行", "pinyin": ["xing2"], "status": "reviewed"}]})

    def test_same_word_two_readings_needs_inline(self):
        text = "华老栓华丽"
        rows = [{"start": 0, "text": "华", "pinyin": ["hua4"], "delivery": "dictionary"},
                {"start": 3, "text": "华", "pinyin": ["hua2"], "delivery": "dictionary"}]
        with self.assertRaisesRegex(ValueError, "inline"):
            self.check({"id": "a", "text": text, "readings": reviewed(text, rows)})
        rows[0]["delivery"] = "inline"
        info = self.check({"id": "a", "text": text, "readings": reviewed(text, rows)})[0]
        self.assertEqual(info["submitted"], "(hua4)老栓华丽")
        self.assertEqual(info["rules"], ["华/(hua2)"])

    def test_dictionary_cannot_override_reviewed_position(self):
        text = "银行行走"
        rows = [{"start": 0, "text": "银行", "pinyin": ["yin2", "hang2"], "delivery": "native"},
                {"start": 2, "text": "行", "pinyin": ["xing2"], "delivery": "dictionary"}]
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.check({"id": "a", "text": text, "readings": reviewed(text, rows)})

    def test_text_rule_cannot_cover_reviewed_polyphone(self):
        text = "银行很好"
        clip = {"id": "a", "text": text, "readings": reviewed(text), "pronunciation_dict": {"tone": ["银行/银杭"]}}
        with self.assertRaisesRegex(ValueError, "文本转写"):
            self.check(clip)

    def test_table_rule_substring_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "子串"):
            self.check({"id": "a", "text": "OpenAPI 和 API"}, {"pronunciation_table": TABLE})
        info = self.check({"id": "a", "text": "api 和 API"}, {"pronunciation_table": TABLE})[0]
        self.assertEqual(info["rules"], ["API/A P I"])

    def test_overlapping_rules_are_rejected(self):
        clip = {"id": "a", "text": "abc", "pronunciation_dict": {"tone": ["ab/x", "bc/y"]}}
        with self.assertRaisesRegex(ValueError, "重叠"):
            self.check(clip)

    def test_marks(self):
        base = {"id": "a", "text": "你好世界"}
        with self.assertRaisesRegex(ValueError, "2.8"):
            self.check({**base, "marks": [{"at": 2, "tag": "laughs"}]}, {"model": "speech-2.6-hd"})
        with self.assertRaisesRegex(ValueError, "可发音"):
            self.check({**base, "marks": [{"at": 4, "pause": 1}]})
        with self.assertRaisesRegex(ValueError, "连续"):
            self.check({**base, "marks": [{"at": 2, "pause": 1}, {"at": 2, "pause": 0.3}]})
        with self.assertRaisesRegex(ValueError, "两位小数"):
            self.check({**base, "marks": [{"at": 2, "pause": 0.333}]})
        info = self.check({**base, "marks": [{"at": 0, "tag": "sighs"}, {"at": 2, "pause": 1.5}]})[0]
        self.assertEqual(info["submitted"], "(sighs)你好<#1.5#>世界")

    def test_mark_inside_reading_is_rejected(self):
        text = "银行"
        rows = [{"start": 0, "text": "银行", "pinyin": ["yin2", "hang2"], "delivery": "native"}]
        with self.assertRaisesRegex(ValueError, "内部"):
            self.check({"id": "a", "text": text, "readings": reviewed(text, rows), "marks": [{"at": 1, "pause": 1}]})

    def test_model_capabilities(self):
        with self.assertRaisesRegex(ValueError, "whisper"):
            self.check({"id": "a", "text": "你好", "emotion": "whisper"})
        self.check({"id": "a", "text": "你好", "emotion": "whisper", "model": "speech-2.6-hd"})
        with self.assertRaisesRegex(ValueError, "quality"):
            self.check({"id": "a", "text": "你好", "text_normalization_mode": "quality", "model": "speech-02-hd"})
        with self.assertRaisesRegex(ValueError, "未知字段"):
            self.check({"id": "a", "text": "你好", "direction": "温柔地说"})

    def test_voice_mapping(self):
        with self.assertRaisesRegex(ValueError, "缺少角色音色"):
            tts.resolve_voices({"clips": [{"id": "a", "role": "甲", "text": "你好"}]})
        with self.assertRaisesRegex(ValueError, "占位"):
            tts.resolve_voices({"voices": {"甲": "替换成你的音色"}, "clips": [{"id": "a", "role": "甲", "text": "你好"}]})
        plan = tts.resolve_voices({"voices": {"甲": "female-tianmei"}, "clips": [{"id": "a", "role": "甲", "text": "你好"}]},
                                  {"甲": "Chinese (Mandarin)_News_Anchor"})
        self.assertEqual(plan["clips"][0]["voice"], "Chinese (Mandarin)_News_Anchor")


class Render(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict("os.environ", {"MINIMAX_API_KEY": "secret-key"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir)

    def run_with(self, fake, plan, **kwargs):
        with mock.patch.object(tts, "call_api", fake.call), mock.patch.object(tts, "download_subtitles", fake.download):
            return tts.render(plan, self.dir, min_interval=0, **kwargs)

    def fixtures(self):
        return {"(hua4)老栓说：<#0.5#>(laughs)这件华丽的衣服，用 API 处理。":
                json.loads((FIXTURES / "probe1.subtitle.json").read_text(encoding="utf-8")),
                "(hua4)(lao3)(shuan1)穿得很华丽。Anthropic 发布 Haiku 5.5，api 和 API。":
                json.loads((FIXTURES / "probe2.subtitle.json").read_text(encoding="utf-8"))}

    def two_clip_plan(self):
        one, two = probe1_plan(), probe2_plan()
        clips = [{**one["clips"][0], "pause_after_ms": 400}, two["clips"][0]]
        return {"title": "两段", "voices": one["voices"], "pronunciation_table": TABLE, "clips": clips}

    def test_dry_run_needs_no_key(self):
        self.env.stop()
        result = tts.render(self.two_clip_plan(), self.dir, dry_run=True)
        self.env.start()
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["billed_characters"], 55 + 67)
        self.assertAlmostEqual(result["estimated_cost_yuan"], 122 * 3.5 / 10000, places=4)
        body = json.loads((self.dir / "requests.json").read_text(encoding="utf-8"))[0]["body"]
        self.assertEqual(body["audio_setting"], {"sample_rate": 24000, "format": "wav", "channel": 1})
        self.assertEqual(body["subtitle_type"], "word")

    def test_render_cache_and_pause_only_change(self):
        fake = Fake(self.fixtures())
        result = self.run_with(fake, self.two_clip_plan())
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(result["timestamps"])
        timeline = json.loads((self.dir / "timeline.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(timeline["clips"][1]["offset"], 6.2 + 0.4)
        second = [w for w in timeline["words"] if w["clip"] == "p2"][0]
        self.assertEqual(second["word"], "华")
        self.assertAlmostEqual(second["startTime"], 6.6 + 0.042667, places=5)
        srt = (self.dir / "final.srt").read_text(encoding="utf-8")
        self.assertIn("华老栓说：", srt)
        self.assertNotIn("(hua4)", srt)
        manifest = (self.dir / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-key", manifest)
        self.assertNotIn("example.invalid", manifest)
        plan = self.two_clip_plan()
        plan["clips"][0]["pause_after_ms"] = 1000
        self.run_with(fake, plan)
        self.assertEqual(len(fake.calls), 2)
        timeline = json.loads((self.dir / "timeline.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(timeline["clips"][1]["offset"], 7.2)

    def test_server_error_is_failed_not_uncertain(self):
        plan = probe1_plan()
        with self.assertRaisesRegex(RuntimeError, "2054"):
            self.run_with(Fake(code=2054), plan)
        entry = list(json.loads((self.dir / "manifest.json").read_text(encoding="utf-8")).values())[0]
        self.assertEqual(entry["status"], "failed")
        fake = Fake(self.fixtures())
        self.run_with(fake, plan)
        self.assertEqual(len(fake.calls), 1)

    def test_uncertain_blocks_until_retry(self):
        plan = probe1_plan()
        def boom(*args, **kwargs):
            raise TimeoutError
        with mock.patch.object(tts, "call_api", boom), self.assertRaisesRegex(RuntimeError, "不确定"):
            tts.render(plan, self.dir, min_interval=0)
        with self.assertRaisesRegex(RuntimeError, "retry-uncertain"):
            self.run_with(Fake(self.fixtures()), plan)
        self.run_with(Fake(self.fixtures()), plan, retry_uncertain=True)

    def test_takes_and_only(self):
        plan = self.two_clip_plan()
        plan["clips"][0]["takes"] = 2
        fake = Fake(self.fixtures())
        result = self.run_with(fake, plan, only=["p1"])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(result["missing_clips"], ["p2"])
        self.assertFalse((self.dir / "final.wav").exists())
        self.assertTrue((self.dir / "takes/p1-take2.wav").exists())
        result = self.run_with(fake, plan)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(result["pending_selection"], ["p1"])
        plan["clips"][0]["selected_take"] = 2
        self.assertEqual(self.run_with(fake, plan)["pending_selection"], [])
        self.assertEqual(len(fake.calls), 3)

    def test_subtitles_disabled(self):
        fake = Fake()
        result = self.run_with(fake, probe1_plan(), timestamps=False)
        self.assertFalse(result["timestamps"])
        self.assertFalse(fake.calls[0]["subtitle_enable"])
        self.assertNotIn("subtitle_type", fake.calls[0])

    def test_preview(self):
        tts.preview(self.two_clip_plan(), self.dir)
        page = (self.dir / "director.html").read_text(encoding="utf-8")
        md = (self.dir / "director.md").read_text(encoding="utf-8")
        self.assertIn("行内拼音", page)
        self.assertIn("〔停顿 0.5 秒〕", md)
        self.assertIn("实际提交文本：(hua4)老栓说：<#0.5#>(laughs)", md)


class Examples(unittest.TestCase):
    def test_examples_validate(self):
        for path in sorted((ROOT / "examples").glob("*/plan.json")):
            with self.subTest(path=path.parent.name):
                plan = json.loads(path.read_text(encoding="utf-8"))
                tts.validate(tts.resolve_voices(plan), complete=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
