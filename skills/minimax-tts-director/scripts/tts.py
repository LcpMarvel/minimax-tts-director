#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pypinyin==0.55.0"]
# ///
"""MiniMax 同步语音合成导演工具：中文多音字校对、行内拼音、发音字典、语气词与停顿标记、阅读稿、WAV/字幕。
prepare / preview / render --dry-run 均不访问网络、不读取密钥。"""
import argparse
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

from pypinyin import Style, lazy_pinyin, pinyin
from pypinyin.constants import PINYIN_DICT
from pypinyin.contrib.tone_convert import to_normal, to_tone

ENDPOINTS = {"cn": "https://api.minimax.cn/v1/t2a_v2", "bj": "https://api-bj.minimaxi.com/v1/t2a_v2"}
KEY_ENV = "MINIMAX_API_KEY"
MODEL = "speech-2.8-hd"
VOICE = "male-qn-jingying"
SAMPLE_RATE = 24000
PRONUNCIATIONS = Path(__file__).resolve().parents[1] / "references/pronunciations.json"
CLIP_LIMIT = 1000
PREPARE_LIMIT = 500
PCM_CHUNK_FRAMES = 24000
DIGITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U000323af]")
SYLLABLES = {to_normal(sound) for sounds in PINYIN_DICT.values() for sound in sounds.split(",")}

BASE_EMOTIONS = ("happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm")
MODELS = {
    # 情绪与语气词标签能力按官方 t2a_v2 文档（2026-11-09 核对）。
    "speech-2.8-hd": {"emotions": BASE_EMOTIONS, "tags": True, "quality": True, "price": 3.5},
    "speech-2.8-turbo": {"emotions": BASE_EMOTIONS, "tags": True, "quality": True, "price": 2.0},
    "speech-2.6-hd": {"emotions": BASE_EMOTIONS + ("fluent", "whisper"), "tags": False, "quality": True, "price": None},
    "speech-2.6-turbo": {"emotions": BASE_EMOTIONS + ("fluent", "whisper"), "tags": False, "quality": True, "price": None},
    "speech-02-hd": {"emotions": BASE_EMOTIONS, "tags": False, "quality": False, "price": None},
    "speech-02-turbo": {"emotions": BASE_EMOTIONS, "tags": False, "quality": False, "price": None},
}
TAGS = {"laughs": "笑声", "chuckle": "轻笑", "coughs": "咳嗽", "clear-throat": "清嗓子", "groans": "呻吟",
        "breath": "换气", "pant": "喘气", "inhale": "吸气", "exhale": "呼气", "gasps": "倒吸气",
        "sniffs": "吸鼻子", "sighs": "叹气", "snorts": "喷鼻息", "burps": "打嗝", "lip-smacking": "咂嘴",
        "humming": "哼唱", "hissing": "嘶嘶声", "emm": "嗯", "sneezes": "喷嚏"}
LANGUAGE_BOOST = {"Chinese", "Chinese,Yue", "English", "Arabic", "Russian", "Spanish", "French", "Portuguese",
                  "German", "Turkish", "Dutch", "Ukrainian", "Vietnamese", "Indonesian", "Japanese", "Italian",
                  "Korean", "Thai", "Polish", "Romanian", "Greek", "Czech", "Finnish", "Hindi", "Bulgarian",
                  "Danish", "Hebrew", "Malay", "Persian", "Slovak", "Swedish", "Croatian", "Filipino",
                  "Hungarian", "Norwegian", "Slovenian", "Catalan", "Nynorsk", "Tamil", "Afrikaans", "auto"}
SOUND_EFFECTS = {"spacious_echo": "空旷回音", "auditorium_echo": "礼堂广播", "lofi_telephone": "电话失真", "robotic": "电音"}
DELIVERIES = {"native": "保留自然读法", "dictionary": "发音字典", "inline": "行内拼音"}
# 服务端明确拒绝的错误码视为未执行；超时与未知错误可能已计费，记为 uncertain。
UNCERTAIN_CODES = {1000, 1001}
CLIP_FIELDS = {"id", "role", "text", "voice", "model", "emotion", "speed", "vol", "pitch", "language_boost",
               "text_normalization_mode", "voice_modify", "pause_after_ms", "marks", "readings",
               "pronunciation_dict", "enable_subtitle", "takes", "selected_take", "note"}
PLAN_FIELDS = {"title", "model", "voices", "enable_subtitle", "language_boost", "text_normalization_mode",
               "pronunciation_table", "clips"}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def check_text(text):
    if not isinstance(text, str) or not (HAN.search(text) or re.search(r"[A-Za-z0-9]", text)):
        raise ValueError("文本须包含中文、英文或数字")
    if any(c.isalpha() and not (HAN.fullmatch(c) or re.fullmatch(r"[A-Za-z]", c)) for c in text):
        raise ValueError("当前支持中文与英文；其他语言须先核实音色能力")
    if re.search(r"<#|#>", text):
        raise ValueError("原文不能包含 <#x#> 停顿标记；停顿写在 marks")
    if re.search(r"\([a-z]+[1-6]\)", text) or re.search(r"\((?:" + "|".join(map(re.escape, TAGS)) + r")\)", text):
        raise ValueError("原文不能包含括号拼音或语气词标签；纠音写在 readings，语气词写在 marks")


def sentence_ends(text):
    ends = []
    for match in re.finditer(r'[。！？!?]+["”’」』）】]*|\n+|\.+["”’」』）】]*', text):
        if match.group().startswith("."):
            # 小数、型号、URL 和常见缩写内部的点不是句末；英文句末须有空白或 EOF。
            if match.end() < len(text) and not text[match.end()].isspace():
                continue
            word = re.search(r"[A-Za-z.]+$", text[:match.start()])
            if word and (word.group().lower() in {"mr", "mrs", "ms", "dr", "prof", "vs", "etc", "e.g", "i.e"}
                         or re.fullmatch(r"(?:[A-Za-z]\.)*[A-Za-z]", word.group())):
                continue
        ends.append(match.end())
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    return ends


def heteronyms(char):
    return pinyin(char, style=Style.TONE3, heteronym=True, neutral_tone_with_five=True)[0]


def scan(text):
    suggested = lazy_pinyin(text, style=Style.TONE3, neutral_tone_with_five=True, errors=lambda s: list(s))
    if len(suggested) != len(text):
        raise ValueError("拼音位置无法与原文对齐，请检查特殊字符")
    return [{"start": i, "text": char, "pinyin": [suggested[i]], "status": "pending",
             "delivery": "native", "note": "词典建议，待语境校对"}
            for i, char in enumerate(text) if HAN.fullmatch(char) and len(heteronyms(char)) > 1]


# ---------- 中英发音表 ----------

def validate_pronunciations(table):
    if table is None:
        return
    if (not isinstance(table, dict) or set(table) != {"version", "words", "acronyms", "models", "decimal_point"}
            or type(table["version"]) is not int or table["version"] != 1):
        raise ValueError("发音表须包含 version=1、words、acronyms、models、decimal_point")
    if not isinstance(table["words"], dict) or not isinstance(table["acronyms"], dict):
        raise ValueError("words 与 acronyms 须为对象")
    for word, ipa in table["words"].items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", word):
            raise ValueError(f"英文词须为不含空格的英文词或型号：{word}")
        if ipa is not None and (not isinstance(ipa, str) or not 1 <= len(ipa) <= 200
                                or re.search(r"[/()\s]", ipa) or HAN.search(ipa)):
            raise ValueError(f"{word} 的读音须为 IPA 字符串（不含括号、斜杠、空白）或 null（原生读法）")
    for word, target in table["acronyms"].items():
        if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,15}", word):
            raise ValueError(f"缩写原词须为大写英文、数字或短横线：{word}")
        if (not isinstance(target, str) or not 1 <= len(target) <= 200
                or not re.fullmatch(r"[A-Za-z0-9 .'-]+", target) or not re.search(r"[A-Za-z]", target)):
            raise ValueError(f"缩写 {word} 的目标须为英文逐字母读法")
    models = table["models"]
    if not isinstance(models, list) or any(not isinstance(word, str) or not re.fullmatch(
            r"[Vv]?[0-9]+(?:\.[0-9]+)+", word) or len(word) > 16 for word in models):
        raise ValueError("models 须为点分数字型号数组，可带 V/v 前缀")
    if len(set(models)) != len(models):
        raise ValueError("型号不可重复")
    if set(table["words"]) & (set(table["acronyms"]) | set(models)) or set(table["acronyms"]) & set(models):
        raise ValueError("同一词只能出现在 words / acronyms / models 之一")
    if table["decimal_point"] not in ("point", "点"):
        raise ValueError("decimal_point 只能为 point 或 点")


def load_pronunciations(path):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError(f"发音表 JSON 字段重复：{name}")
            result[name] = value
        return result
    table = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    if table is None:
        raise ValueError("发音表文件须为对象")
    validate_pronunciations(table)
    return table


def token_starts(text, word, model=False):
    boundary = r"[A-Za-z0-9_.]" if model else r"[A-Za-z0-9_]"
    return {m.start() for m in re.finditer(rf"(?<!{boundary}){re.escape(word)}(?!{boundary})", text)}


def english_integer(digits):
    if len(digits) > 1 and digits.startswith("0"):
        return " ".join(DIGITS[int(d)] for d in digits)
    number = int(digits)
    small = DIGITS + ("ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
                      "sixteen", "seventeen", "eighteen", "nineteen")
    if number < 20:
        return small[number]
    if number < 100:
        tens, rest = divmod(number, 10)
        return ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")[tens - 2] + (
            " " + small[rest] if rest else "")
    for divisor, label in ((1000000, "million"), (1000, "thousand"), (100, "hundred")):
        if number >= divisor:
            first, rest = divmod(number, divisor)
            return english_integer(str(first)) + " " + label + (" " + english_integer(str(rest)) if rest else "")


def table_rules(text, table):
    """返回 {原词: 目标} 与 {原词: 预期起点}；只编译正文实际出现（按词界）的规则。"""
    rules, intended = {}, {}
    if table is None:
        return rules, intended
    targets = {word: "(" + ipa + ")" for word, ipa in table["words"].items() if ipa is not None}
    targets.update(table["acronyms"])
    for model in table["models"]:
        parts = model.lstrip("Vv").split(".")
        if table["decimal_point"] == "point":
            spoken = " point ".join([english_integer(parts[0])] + [
                " ".join(DIGITS[int(d)] for d in part) for part in parts[1:]])
        else:
            spoken = "点".join(parts)
        targets[model] = (model[0] + " " if model[0] in "Vv" else "") + spoken
    for word, target in sorted(targets.items()):
        starts = token_starts(text, word, word in table["models"])
        if starts:
            rules[word], intended[word] = target, starts
    return rules, intended


# ---------- 片段校验与提交文本 ----------

def delivery_of(row):
    if "delivery" not in row:
        raise ValueError(f"readings 项须显式写 delivery（native / dictionary / inline）：{row.get('text')}")
    if row["delivery"] not in DELIVERIES:
        raise ValueError(f"未知读音投递方式：{row['delivery']}")
    return row["delivery"]


def check_readings(clip, complete=False):
    text, occupied, rows = clip["text"], set(), []
    raw = clip.get("readings", [])
    if not isinstance(raw, list):
        raise ValueError("readings 须为数组")
    for row in sorted(raw, key=lambda r: r.get("start", -1) if isinstance(r, dict) else -1):
        if not isinstance(row, dict) or set(row) - {"start", "text", "pinyin", "status", "delivery", "note"}:
            raise ValueError("readings 项只允许 start/text/pinyin/status/delivery/note")
        start, word, sounds = row.get("start"), row.get("text"), row.get("pinyin")
        if type(start) is not int or start < 0 or not isinstance(word, str) or not 1 <= len(word) <= 16:
            raise ValueError(f"标注位置或词组长度无效：{word}")
        if not all(HAN.fullmatch(c) for c in word) or text[start:start + len(word)] != word:
            raise ValueError(f"标注与原文不符：{word}@{start}")
        if not isinstance(sounds, list) or len(sounds) != len(word) or any(
                not isinstance(s, str) or not re.fullmatch(r"[a-zv]+[1-5]", s) or to_normal(s) not in SYLLABLES
                for s in sounds):
            raise ValueError(f"每个字须有一个数字声调拼音（轻声用 5，ü 用 v）：{word}")
        positions = set(range(start, start + len(word)))
        if positions & occupied:
            raise ValueError(f"标注重叠：{word}@{start}")
        occupied |= positions
        if row.get("status") not in ("pending", "reviewed"):
            raise ValueError("status 只能为 pending 或 reviewed")
        delivery_of(row)
        if row["status"] != "reviewed" and complete:
            raise ValueError(f"尚未校对：{word}@{start}")
        rows.append(row)
    if complete:
        reviewed = {r["start"] + j for r in rows if r["status"] == "reviewed" for j in range(len(r["text"]))}
        missing = [r for r in scan(text) if r["start"] not in reviewed]
        if missing:
            raise ValueError(f"多音字缺少已校对标注：{missing[0]['text']}@{missing[0]['start']}")
    return rows


def check_marks(clip, model, rows):
    text, marks = clip["text"], clip.get("marks", [])
    if not isinstance(marks, list):
        raise ValueError("marks 须为数组")
    inside = {r["start"] + j for r in rows for j in range(1, len(r["text"]))}
    pauses = set()
    for mark in marks:
        if not isinstance(mark, dict) or set(mark) - {"at", "tag", "pause", "note"} or ("tag" in mark) == ("pause" in mark):
            raise ValueError("marks 项须为 {at, tag} 或 {at, pause}，可附 note")
        at = mark["at"]
        if type(at) is not int or not 0 <= at <= len(text):
            raise ValueError(f"marks.at 须为 0～{len(text)} 的整数")
        if at in inside:
            raise ValueError(f"marks.at={at} 落在已标注词组内部")
        if "tag" in mark:
            if mark["tag"] not in TAGS:
                raise ValueError(f"未知语气词标签：{mark['tag']}；可选 " + "、".join(TAGS))
            if not MODELS[model]["tags"]:
                raise ValueError(f"语气词标签仅支持 speech-2.8-hd / speech-2.8-turbo，当前 {model}")
        else:
            pause = mark["pause"]
            if type(pause) not in (int, float) or not 0.01 <= pause <= 99.99 or round(pause, 2) != pause:
                raise ValueError("pause 须为 0.01～99.99 秒，最多两位小数")
            # 官方要求停顿位于两段可发音文本之间，且不能连续使用多个停顿。
            if not re.search(r"[A-Za-z0-9]|" + HAN.pattern, text[:at]) or not re.search(
                    r"[A-Za-z0-9]|" + HAN.pattern, text[at:]):
                raise ValueError(f"停顿 at={at} 须位于两段可发音文本之间")
            if at in pauses:
                raise ValueError(f"同一位置 at={at} 不能连续使用多个停顿")
            pauses.add(at)
    return marks


def compose(clip, rows, marks):
    """原文 → 实际提交文本。origin[i] 为提交文本第 i 个码点的来源：("text", 原位置) / ("inline", 原位置) / ("mark", 序号)。"""
    text = clip["text"]
    inline = {}
    for row in rows:
        if row["status"] == "reviewed" and row["delivery"] == "inline":
            for j, sound in enumerate(row["pinyin"]):
                inline[row["start"] + j] = "(" + sound + ")"
    by_at = {}
    for index, mark in enumerate(marks):
        by_at.setdefault(mark["at"], []).append(index)
    out, origin = [], []

    def emit_marks(at):
        for index in by_at.get(at, []):
            mark = marks[index]
            token = "(" + mark["tag"] + ")" if "tag" in mark else "<#" + format(mark["pause"], "g") + "#>"
            out.append(token)
            origin.extend([("mark", index)] * len(token))

    for i, char in enumerate(text):
        emit_marks(i)
        token = inline.get(i, char)
        out.append(token)
        origin.extend([("inline" if i in inline else "text", i)] * len(token))
    emit_marks(len(text))
    return "".join(out), origin


def parse_dict_entry(entry):
    if not isinstance(entry, str) or entry.count("/") != 1:
        raise ValueError(f"发音字典规则须为 原词/替换内容：{entry}")
    word, target = entry.split("/")
    if not word or any(c.isspace() for c in word) or "<#" in word or "(" in word:
        raise ValueError(f"字典原词不能为空、含空白、括号或停顿标记：{entry}")
    if not target or len(target) > 500 or "<#" in target:
        raise ValueError(f"字典替换内容须为 1～500 字符且不含停顿标记：{entry}")
    sounds = re.findall(r"\(([a-zv]+[1-5])\)", target)
    if HAN.fullmatch(word[0]) and all(HAN.fullmatch(c) for c in word) and "".join(f"({s})" for s in sounds) == target:
        if len(sounds) != len(word) or any(to_normal(s) not in SYLLABLES for s in sounds):
            raise ValueError(f"括号拼音规则须逐字一个数字声调拼音：{entry}")
        return word, sounds
    return word, target


def compile_rules(clip, rows, submitted, origin, table):
    """合并 readings 字典、片段补充字典与发音表；保守校验所有命中位置。"""
    text = clip["text"]
    rules, kinds = {}, {}

    def add(word, value, kind):
        if word in rules and rules[word] != value:
            raise ValueError(f"同一原词有不同规则：{word}；同词异读请改用 delivery=inline")
        rules[word], kinds[word] = value, kinds.get(word, kind)

    for row in rows:
        if row["status"] == "reviewed" and row["delivery"] == "dictionary":
            add(row["text"], list(row["pinyin"]), "reading")
    extra = clip.get("pronunciation_dict", {"tone": []})
    if not isinstance(extra, dict) or set(extra) != {"tone"} or not isinstance(extra["tone"], list):
        raise ValueError("pronunciation_dict 须为 {\"tone\": [...]}")
    for entry in extra["tone"]:
        word, value = parse_dict_entry(entry)
        add(word, value, "manual")
    generated, intended = table_rules(text, table)
    for word, value in generated.items():
        add(word, value, "table")
    known = {r["start"] + j: s for r in rows for j, s in enumerate(r["pinyin"]) if r["status"] == "reviewed"}
    spans = []
    for word, value in rules.items():
        found, start = False, submitted.find(word)
        while start >= 0:
            found = True
            sources = origin[start:start + len(word)]
            if any(kind != "text" for kind, _ in sources):
                raise ValueError(f"字典规则 {word} 会命中行内拼音或插入标记；调整规则或标注")
            first = sources[0][1]
            if kinds[word] == "table" and first not in intended[word]:
                raise ValueError(f"发音规则 {word} 会误改另一词或型号的子串；改为完整词组或拆段")
            for j in range(len(word)):
                pos = first + j
                if pos in known:
                    if not isinstance(value, list):
                        raise ValueError(f"文本转写 {word} 覆盖了已校对的多音字 {text[pos]}@{pos}")
                    if value[j] != known[pos]:
                        raise ValueError(f"字典规则 {word} 与 {text[pos]}@{pos} 的校对读音冲突；改用 inline")
            spans.append((start, start + len(word), word))
            start = submitted.find(word, start + 1)
        if not found and kinds[word] != "manual":
            raise ValueError(f"字典规则 {word} 在提交文本中没有命中")
    spans.sort()
    for (a_start, a_end, a_word), (b_start, b_end, b_word) in zip(spans, spans[1:]):
        if b_start < a_end and a_word != b_word:
            # 官方未说明多条规则重叠时的优先级，不猜测。
            raise ValueError(f"字典规则 {a_word} 与 {b_word} 命中位置重叠；合并成一条完整词组规则")
    return [w + "/" + ("".join(f"({s})" for s in v) if isinstance(v, list) else v) for w, v in rules.items()]


def check_voice(voice):
    if not isinstance(voice, str) or "替换" in voice or "你的" in voice:
        raise ValueError("音色 ID 是占位值，请填写自己的音色")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-() .,]{1,255}", voice):
        raise ValueError(f"音色 ID 格式无效：{voice}")


def resolve_voices(plan, voices=None):
    mapping = {}
    for source in (plan.get("voices", {}), voices if voices is not None else {}):
        if not isinstance(source, dict) or any(not isinstance(r, str) or not r.strip() for r in source):
            raise ValueError("voices 须为角色到音色 ID 的对象")
        mapping.update(source)
    for voice in mapping.values():
        check_voice(voice)
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("计划需要非空 clips")
    resolved, missing = [], set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("clips 项须为对象")
        item = dict(clip)
        if "voice" not in item:
            role = item.get("role", "旁白")
            if role in mapping:
                item["voice"] = mapping[role]
            else:
                missing.add(role)
        resolved.append(item)
    if missing:
        raise ValueError("缺少角色音色，请在 voices 填写：" + "、".join(sorted(map(str, missing))))
    return {**plan, "voices": mapping, "clips": resolved}


def setting(plan, clip, name, default=None):
    return clip[name] if name in clip else plan.get(name, default)


def check_clip(plan, clip, complete):
    unknown = set(clip) - CLIP_FIELDS
    if unknown:
        raise ValueError(f"片段 {clip.get('id')} 有未知字段：" + "、".join(sorted(unknown)))
    model = setting(plan, clip, "model", MODEL)
    if model not in MODELS:
        raise ValueError(f"不支持的模型：{model}；可选 " + "、".join(MODELS))
    check_text(clip.get("text"))
    if len(clip["text"]) > CLIP_LIMIT:
        raise ValueError(f"单段本地上限 {CLIP_LIMIT} 字；请明确分段，不截断")
    check_voice(clip["voice"])
    role = clip.get("role", "旁白")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role 必须为非空字符串")
    emotion = clip.get("emotion")
    if emotion is not None and emotion not in MODELS[model]["emotions"]:
        raise ValueError(f"{model} 不支持情绪 {emotion}；可选 " + "、".join(MODELS[model]["emotions"]))
    speed, vol, pitch = clip.get("speed", 1), clip.get("vol", 1), clip.get("pitch", 0)
    if type(speed) not in (int, float) or not 0.5 <= speed <= 2:
        raise ValueError("speed 须为 0.5～2")
    if type(vol) not in (int, float) or not 0 < vol <= 10:
        raise ValueError("vol 须为 (0, 10]")
    if type(pitch) is not int or not -12 <= pitch <= 12:
        raise ValueError("pitch 须为 -12～12 的整数")
    pause = clip.get("pause_after_ms", 0)
    if type(pause) is not int or not 0 <= pause <= 30000:
        raise ValueError("pause_after_ms 须为 0～30000 的整数")
    boost = setting(plan, clip, "language_boost")
    if boost is not None and boost not in LANGUAGE_BOOST:
        raise ValueError(f"language_boost 无效：{boost}")
    mode = setting(plan, clip, "text_normalization_mode")
    if mode is not None and mode not in ("basic", "quality"):
        raise ValueError("text_normalization_mode 只能为 basic 或 quality")
    if mode == "quality" and not MODELS[model]["quality"]:
        raise ValueError(f"{model} 不支持 quality 文本归一化（服务端会静默降级为 basic）")
    modify = clip.get("voice_modify")
    if modify is not None:
        if not isinstance(modify, dict) or not modify or set(modify) - {"pitch", "intensity", "timbre", "sound_effects"}:
            raise ValueError("voice_modify 只允许 pitch / intensity / timbre / sound_effects")
        for name in ("pitch", "intensity", "timbre"):
            if name in modify and (type(modify[name]) is not int or not -100 <= modify[name] <= 100):
                raise ValueError(f"voice_modify.{name} 须为 -100～100 的整数")
        if "sound_effects" in modify and modify["sound_effects"] not in SOUND_EFFECTS:
            raise ValueError("voice_modify.sound_effects 可选 " + "、".join(SOUND_EFFECTS))
    if "enable_subtitle" in clip and type(clip["enable_subtitle"]) is not bool:
        raise ValueError("enable_subtitle 必须为布尔值")
    takes, selected = clip.get("takes", 1), clip.get("selected_take")
    if type(takes) is not int or not 1 <= takes <= 4:
        raise ValueError("takes 须为 1～4 的整数")
    if selected is not None and (type(selected) is not int or not 1 <= selected <= takes):
        raise ValueError("selected_take 须为 null 或 1～takes 的整数")
    if not isinstance(clip.get("note", ""), str):
        raise ValueError("note 必须为字符串")
    rows = check_readings(clip, complete)
    marks = check_marks(clip, model, rows)
    submitted, origin = compose(clip, rows, marks)
    rules = compile_rules(clip, rows, submitted, origin, plan.get("pronunciation_table"))
    return {"model": model, "rows": rows, "marks": marks, "submitted": submitted, "origin": origin, "rules": rules}


def validate(plan, complete=False):
    if not isinstance(plan, dict):
        raise ValueError("计划须为 JSON 对象")
    unknown = set(plan) - PLAN_FIELDS
    if unknown:
        raise ValueError("计划有未知顶层字段：" + "、".join(sorted(unknown)))
    validate_pronunciations(plan.get("pronunciation_table"))
    if "enable_subtitle" in plan and type(plan["enable_subtitle"]) is not bool:
        raise ValueError("enable_subtitle 必须为布尔值")
    ids, checked = set(), []
    for clip in plan["clips"]:
        ident = clip.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", ident) or ident in ids:
            raise ValueError("片段 id 必须唯一且只包含字母数字下划线短横线")
        ids.add(ident)
        checked.append(check_clip(plan, clip, complete))
    return checked


def request_body(plan, clip, info, enable_subtitle):
    voice = {"voice_id": clip["voice"], "speed": clip.get("speed", 1), "vol": clip.get("vol", 1),
             "pitch": clip.get("pitch", 0)}
    if clip.get("emotion"):
        voice["emotion"] = clip["emotion"]
    body = {"model": info["model"], "text": info["submitted"], "stream": False, "voice_setting": voice,
            "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "wav", "channel": 1},
            "subtitle_enable": enable_subtitle, "output_format": "hex"}
    if enable_subtitle:
        body["subtitle_type"] = "word"
    if info["rules"]:
        body["pronunciation_dict"] = {"tone": info["rules"]}
    for name in ("language_boost", "text_normalization_mode"):
        value = setting(plan, clip, name)
        if value is not None:
            body[name] = value
    if clip.get("voice_modify"):
        body["voice_modify"] = clip["voice_modify"]
    return body


def billed_characters(text):
    # 官方计费：1 个汉字算 2 个字符，其余字符各算 1 个；已用真实 usage_characters 核对。
    return sum(2 if HAN.fullmatch(c) else 1 for c in text)


def fingerprint(body, take):
    value = body if take == 1 else {"body": body, "take": take}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------- prepare / preview ----------

def prepare(text, voice=VOICE, model=MODEL, pronunciation_table=None):
    check_voice(voice)
    if model not in MODELS:
        raise ValueError(f"不支持的模型：{model}")
    check_text(text)
    parts, current, start = [], "", 0
    for end in sentence_ends(text):
        sentence, start = text[start:end], end
        if len(sentence) > PREPARE_LIMIT:
            raise ValueError(f"有超过 {PREPARE_LIMIT} 字的连续句子，请明确分段")
        if len(current) + len(sentence) > PREPARE_LIMIT:
            parts.append(current)
            current = ""
        current += sentence
    if current:
        parts.append(current)
    if "".join(parts) != text:
        raise ValueError("分段未保留完整原文")
    clips = [{"id": f"{i:03}", "role": "旁白", "text": part, "voice": voice, "pause_after_ms": 0,
              "readings": scan(part)} for i, part in enumerate(parts, 1)]
    english = bool(re.search(r"[A-Za-z]", text))
    table = pronunciation_table
    if table is None and english:
        table = load_pronunciations(PRONUNCIATIONS)
    validate_pronunciations(table)
    plan = {"title": "中英阅读" if english else "中文阅读", "model": model, "clips": clips}
    if table is not None:
        plan["pronunciation_table"] = json.loads(json.dumps(table))
    return plan


def mark_label(mark):
    if "tag" in mark:
        return f"{TAGS[mark['tag']]} ({mark['tag']})"
    return f"停顿 {format(mark['pause'], 'g')} 秒"


def preview(plan, out, voices=None):
    plan = resolve_voices(plan, voices)
    checked = validate(plan)
    out.mkdir(parents=True, exist_ok=True)
    title = str(plan.get("title", "阅读稿"))
    md, sections = [f"# {title}\n\n读音与标记为审稿标注；“实际提交文本”才是发给 MiniMax 的内容。待校对项尚未确定。\n"], []
    table = plan.get("pronunciation_table")
    for clip, info in zip(plan["clips"], checked):
        rows = list(info["rows"])
        covered = {r["start"] + j for r in rows for j in range(len(r["text"]))}
        rows += [r for r in scan(clip["text"]) if r["start"] not in covered]
        rows.sort(key=lambda r: r["start"])
        marks_at = {}
        for mark in info["marks"]:
            marks_at.setdefault(mark["at"], []).append(mark)
        markup, plain, cells, details = [], [], [], []
        by_start = {r["start"]: r for r in rows}
        i, text = 0, clip["text"]
        while i <= len(text):
            for mark in marks_at.get(i, []):
                markup.append(f'<span class=mark>〔{html.escape(mark_label(mark))}〕</span>')
                plain.append(f"〔{mark_label(mark)}〕")
            if i == len(text):
                break
            row = by_start.get(i)
            if row is None:
                markup.append(html.escape(text[i]))
                plain.append(text[i])
                i += 1
                continue
            word = row["text"]
            sound = " ".join(to_tone(s) for s in row["pinyin"])
            status = "已校对" if row["status"] == "reviewed" else "待校对"
            options = "；".join(c + "：" + "/".join(to_tone(p) for p in heteronyms(c)) for c in word)
            channel = DELIVERIES[row["delivery"]]
            markup.append(f'<ruby class="{row["status"]} {row["delivery"]}" title="{html.escape(status + "；" + channel + "；" + options, quote=True)}">'
                          + html.escape(word) + "<rt>" + html.escape(sound) + "</rt></ruby>")
            plain.append(f"{word}〔{sound} · {status} · {channel}〕")
            context = text[max(0, i - 8):i + len(word) + 8]
            values = [word, sound, status, channel, context, options, str(row.get("note", ""))]
            cells.append("<tr>" + "".join("<td>" + html.escape(v) + "</td>" for v in values) + "</tr>")
            details.append(f"- @{i} {word}：{sound}；{status}；{channel}；语境：{context}；备选：{options}；{row.get('note', '')}\n")
            i += len(word)
        modify = clip.get("voice_modify") or {}
        native = sorted(w for w, ipa in (table or {}).get("words", {}).items()
                        if ipa is None and token_starts(text, w))
        controls = [("模型", info["model"]),
                    ("情绪", clip.get("emotion", "未指定（模型按文本自动匹配）")),
                    ("语速 / 音量 / 语调", f"{clip.get('speed', 1)} / {clip.get('vol', 1)} / {clip.get('pitch', 0)}"),
                    ("语种增强", str(setting(plan, clip, "language_boost") or "")),
                    ("文本归一化", str(setting(plan, clip, "text_normalization_mode") or "")),
                    ("声音效果器", "、".join(f"{k}={SOUND_EFFECTS.get(v, v)}" for k, v in modify.items())),
                    ("段后停顿", f"{clip.get('pause_after_ms', 0)} 毫秒" if clip.get("pause_after_ms") else ""),
                    ("takes", str(clip.get("takes", 1)) if clip.get("takes", 1) > 1 else ""),
                    ("导演备注（不发送）", clip.get("note", "")),
                    ("英语原生读法", "、".join(native)),
                    ("发音字典（服务端按词全局生效）", "\n".join(info["rules"])),
                    ("实际提交文本", info["submitted"]),
                    ("计费字符", str(billed_characters(info["submitted"])))]
        label = f'{clip["id"]} · {clip.get("role", "旁白")} · {clip["voice"]}'
        md += ["\n## " + label + "\n", "".join(plain) + "\n"]
        md.extend(name + "：" + value + "\n" for name, value in controls if value)
        md.extend(details)
        sections.append("<section><h2>" + html.escape(label) + "</h2><p class=script>" + "".join(markup) + "</p>"
                        + "".join("<p class=control><b>" + html.escape(name) + "</b>：" + html.escape(value) + "</p>"
                                  for name, value in controls if value)
                        + "<div class=table><table><thead><tr>"
                        + "".join("<th>" + h + "</th>" for h in ["字词", "读音", "状态", "合成处理", "语境", "备选读音", "理由"])
                        + "</tr></thead><tbody>" + "".join(cells) + "</tbody></table></div></section>")
    (out / "director.md").write_text("".join(md), encoding="utf-8")
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>''' + html.escape(title) + '''</title><style>
body{max-width:1040px;margin:40px auto;padding:0 24px;color:#203038;background:#f7f5ef;font-family:system-ui,sans-serif}
section{background:#fff;border:1px solid #deded5;border-radius:12px;padding:24px;margin:24px 0}
h2{font-size:15px;color:#526369}.script{font-size:23px;line-height:2.5;white-space:pre-wrap}
ruby.reviewed{background:#e3f1e9}ruby.pending{background:#ffe8b4}ruby.inline{box-shadow:inset 0 -2px #2f7d6d}
ruby.dictionary{box-shadow:inset 0 -2px #6b5ca5}rt{font-size:12px;color:#435d59}
.mark{font-size:14px;color:#9a4b1c;background:#fbe9dc;border-radius:4px;padding:0 4px}
.control{white-space:pre-wrap;font-size:14px;margin:6px 0}table{border-collapse:collapse;width:100%;font-size:13px}
td,th{text-align:left;padding:10px;border-bottom:1px solid #eee}.table{overflow:auto}button{padding:8px 16px;cursor:pointer}
.hide rt{visibility:hidden}@media(max-width:600px){body{padding:0 16px}section{padding:16px}}</style>
<h1>''' + html.escape(title) + '''</h1><p>黄色待校对，绿色已校对；绿色下划线为行内拼音，紫色下划线为发音字典，橙色为语气词与停顿标记。本页不证明已生成音频。</p>
<button onclick="document.body.classList.toggle('hide')">显示／隐藏注音</button>''' + "".join(sections) + "</html>"
    (out / "director.html").write_text(page, encoding="utf-8")


# ---------- 字幕 ----------

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_subtitles(raw, clip, info, duration):
    """把 subtitle_file 的分段与词级时间映射回原文。位置为提交文本的码点下标（2026-11-09 实测）。"""
    if not isinstance(raw, list):
        raise ValueError("字幕文件须为分段数组")
    submitted, origin, text = info["submitted"], info["origin"], clip["text"]
    words, sentences, tags = [], [], []
    limit = duration * 1000 + 50

    def span_ms(item):
        start, end = item.get("time_begin"), item.get("time_end")
        if not finite(start) or not finite(end) or not 0 <= start <= end <= limit:
            raise ValueError("字幕时间无效或超过音频时长")
        return start, end

    for segment in raw:
        if not isinstance(segment, dict) or not isinstance(segment.get("timestamped_words"), list):
            raise ValueError("字幕分段缺少 timestamped_words")
        seg_start, seg_end = span_ms(segment)
        groups = {}
        for item in segment["timestamped_words"]:
            begin, end = item.get("word_begin"), item.get("word_end")
            if type(begin) is not int or type(end) is not int or not 0 <= begin < end <= len(submitted):
                raise ValueError("字幕字词位置无效")
            if submitted[begin:end] != item.get("word"):
                raise ValueError("字幕字词与提交文本位置不一致")
            left, right = span_ms(item)
            sound = item.get("pronounce_word") if isinstance(item.get("pronounce_word"), str) else ""
            old = groups.get((begin, end))
            # 行内拼音与字典词组会按注音符号拆成多条，合并时间并拼回服务端注音。
            groups[(begin, end)] = (min(left, old[0]), max(right, old[1]), old[2] + sound) if old else (left, right, sound)
        local = []
        for (begin, end), (left, right, sound) in sorted(groups.items()):
            sources = origin[begin:end]
            positions = [pos for kind, pos in sources if kind != "mark"]
            if not positions:
                mark = info["marks"][sources[0][1]]
                if "tag" in mark:
                    tags.append({"tag": mark["tag"], "at": mark["at"], "startTime": left / 1000, "endTime": right / 1000})
                continue
            first, last = min(positions), max(positions) + 1
            word = {"word": text[first:last], "start": first, "end": last,
                    "startTime": left / 1000, "endTime": right / 1000}
            if submitted[begin:end] != word["word"]:
                word["submitted"] = submitted[begin:end]
            if sound and sound != submitted[begin:end]:
                word["pronounce"] = sound
            local.append(word)
        if local:
            sentences.append({"text": text[local[0]["start"]:local[-1]["end"]],
                              "startTime": seg_start / 1000, "endTime": seg_end / 1000})
            words.extend(local)
    if any(a["endTime"] > b["startTime"] + 1e-6 for a, b in zip(sentences, sentences[1:])):
        raise ValueError("字幕句子时间重叠或乱序")
    if any(a["start"] >= b["start"] or a["startTime"] > b["startTime"] for a, b in zip(words, words[1:])):
        raise ValueError("字幕字词乱序")
    return {"text": text, "submitted_text": submitted, "words": words, "sentences": sentences, "tags": tags}


def empty_subtitles(clip, info):
    return {"text": clip["text"], "submitted_text": info["submitted"], "words": [], "sentences": [], "tags": []}


def check_cached_subtitles(data, clip, info, duration):
    if (not isinstance(data, dict) or data.get("text") != clip["text"]
            or data.get("submitted_text") != info["submitted"]
            or not all(isinstance(data.get(n), list) for n in ("words", "sentences", "tags"))):
        raise ValueError("字幕缓存与片段文本不一致")
    for item in data["words"] + data["sentences"] + data["tags"]:
        if (not isinstance(item, dict) or not finite(item.get("startTime")) or not finite(item.get("endTime"))
                or not 0 <= item["startTime"] <= item["endTime"] <= duration + 0.05):
            raise ValueError("字幕缓存时间无效")
    for word in data["words"]:
        if clip["text"][word["start"]:word["end"]] != word["word"]:
            raise ValueError("字幕缓存字词与原文位置不一致")
    return data


# ---------- 音频与成品 ----------

def decode_wav(raw):
    with wave.open(io.BytesIO(raw), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("MiniMax 须返回 24000 Hz、16 bit、单声道 WAV")
        frames = wav.getnframes()
        pcm = wav.readframes(frames)
    if frames <= 0 or len(pcm) != frames * 2:
        raise ValueError("返回的 WAV 为空或不完整")
    return pcm


def wav_frames(path):
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("缓存音频格式错误")
        frames = wav.getnframes()
        remaining = frames
        while remaining:
            count = min(remaining, PCM_CHUNK_FRAMES)
            if len(wav.readframes(count)) != count * 2:
                raise ValueError("缓存音频不完整")
            remaining -= count
    if frames <= 0:
        raise ValueError("缓存音频为空")
    return frames


def write_wav(path, pcm):
    temp = path.with_name(path.name + ".tmp")
    with wave.open(str(temp), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    temp.replace(path)


def concatenate(segments, final_path):
    temp = final_path.with_name(final_path.name + ".tmp")
    silence = b"\0\0" * PCM_CHUNK_FRAMES
    try:
        with wave.open(str(temp), "wb") as final:
            final.setnchannels(1)
            final.setsampwidth(2)
            final.setframerate(SAMPLE_RATE)
            for segment in segments:
                with wave.open(str(final_path.parent / segment["audio"]), "rb") as wav:
                    remaining = segment["frames"]
                    while remaining:
                        count = min(remaining, PCM_CHUNK_FRAMES)
                        final.writeframesraw(wav.readframes(count))
                        remaining -= count
                remaining = SAMPLE_RATE * segment["clip"].get("pause_after_ms", 0) // 1000
                while remaining:
                    count = min(remaining, PCM_CHUNK_FRAMES)
                    final.writeframesraw(silence[:count * 2])
                    remaining -= count
        temp.replace(final_path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def build_timeline(segments):
    frames, clips, words, sentences, tags = 0, [], [], [], []
    for segment in segments:
        clip, data = segment["clip"], segment["subtitles"]
        offset = frames / SAMPLE_RATE
        clips.append({"id": clip["id"], "role": clip.get("role", "旁白"), "text": clip["text"],
                      "submitted_text": data["submitted_text"], "offset": offset,
                      "duration": segment["frames"] / SAMPLE_RATE, "pause_after_ms": clip.get("pause_after_ms", 0),
                      "audio": segment["audio"], "words_file": segment["words_file"],
                      "timestamps": bool(data["words"]), "timestamps_reason": segment["reason"],
                      "take": segment["take"], "take_selection": segment["selection"]})
        for name, target in (("words", words), ("sentences", sentences), ("tags", tags)):
            for item in data[name]:
                target.append({**item, "clip": clip["id"], "startTime": round(offset + item["startTime"], 6),
                               "endTime": round(offset + item["endTime"], 6)})
        frames += segment["frames"] + SAMPLE_RATE * clip.get("pause_after_ms", 0) // 1000
    return {"sample_rate": SAMPLE_RATE, "duration": frames / SAMPLE_RATE,
            "timestamps": all(c["timestamps"] for c in clips), "clips": clips,
            "words": words, "sentences": sentences, "tags": tags}


def srt_text(timeline):
    def stamp(seconds):
        ms = round(seconds * 1000)
        sec, ms = divmod(ms, 1000)
        minute, sec = divmod(sec, 60)
        hour, minute = divmod(minute, 60)
        return f"{hour:02}:{minute:02}:{sec:02},{ms:03}"
    return "".join(f"{i}\n{stamp(c['startTime'])} --> {stamp(c['endTime'])}\n" + " ".join(c["text"].split()) + "\n\n"
                   for i, c in enumerate(timeline["sentences"], 1))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def redact(payload, key):
    def walk(value):
        if isinstance(value, dict):
            return {k: (f"[hex {len(v)} chars]" if k == "audio" and isinstance(v, str) else
                        "[url redacted]" if k == "subtitle_file" and v else walk(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str) and key:
            return value.replace(key, "[REDACTED]")
        return value
    return walk(payload)


def call_api(endpoint, body, key, timeout):
    request = urllib.request.Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    # 不跟随重定向，避免凭据转发到其他目的地。
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        return response.status, response.headers.get("Trace-Id"), json.load(response)


def download_subtitles(url, timeout):
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("字幕链接须为 https")
    # 字幕文件为预签名对象存储链接，不携带 API 密钥。
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        return json.load(response)


def render(plan, out, dry_run=False, retry_uncertain=False, timestamps=None, voices=None, only=None,
           endpoint=ENDPOINTS["cn"], min_interval=3.5, debug=False):
    plan = resolve_voices(plan, voices)
    checked = validate(plan, complete=True)
    if endpoint not in ENDPOINTS.values():
        raise ValueError("只允许官方接入地址：" + "、".join(ENDPOINTS.values()))
    ids = [clip["id"] for clip in plan["clips"]]
    if only is not None:
        unknown = set(only) - set(ids)
        if unknown:
            raise ValueError("--only 包含不存在的片段：" + "、".join(sorted(unknown)))
    jobs, attempts = [], []
    for clip, info in zip(plan["clips"], checked):
        enabled = timestamps if timestamps is not None else clip.get("enable_subtitle", plan.get("enable_subtitle", True))
        body = request_body(plan, clip, info, enabled)
        jobs.append((clip, info, body))
        for take in range(1, clip.get("takes", 1) + 1):
            attempts.append((clip, info, body, take, fingerprint(body, take)))
    out.mkdir(parents=True, exist_ok=True)
    save(out / "requests.json", [{"clip": c["id"], "takes": c.get("takes", 1), "endpoint": endpoint, "body": b}
                                 for c, _, b in jobs])
    if dry_run:
        selected = [a for a in attempts if only is None or a[0]["id"] in only]
        characters = sum(billed_characters(b["text"]) for _, _, b, _, _ in selected)
        cost = 0.0
        for _, info, body, _, _ in selected:
            price = MODELS[info["model"]]["price"]
            cost = None if price is None or cost is None else cost + billed_characters(body["text"]) * price / 10000
        return {"dry_run": True, "clips": len(jobs), "calls": len(selected), "billed_characters": characters,
                "estimated_cost_yuan": None if cost is None else round(cost, 4),
                "requests": str((out / "requests.json").resolve()),
                "note": "calls 为不考虑缓存命中的调用上界；计费字符 = 汉字×2 + 其他字符×1（按实际提交文本），"
                        "价格按官方定价页 2026-11-09 的 speech-2.8 系列，以控制台为准"}
    key = os.environ.get(KEY_ENV)
    if not key:
        raise ValueError(f"运行环境缺少 {KEY_ENV}")
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    cached = {}
    # 任何新调用前先校验全部已完成缓存，避免调用一半才发现后段缓存损坏。
    for clip, info, body, take, fp in attempts:
        entry = manifest.get(fp, {})
        eligible = only is None or clip["id"] in only
        if eligible and entry.get("status") == "uncertain" and not retry_uncertain:
            raise RuntimeError(f"片段 {clip['id']} take {take} 有结果不确定的请求（可能已计费）；"
                               "核对控制台调用记录与授权后才能 --retry-uncertain")
        if entry.get("status") == "complete" and fp not in cached:
            try:
                frames = wav_frames(out / (fp + ".wav"))
                data = check_cached_subtitles(json.loads((out / (fp + ".words.json")).read_text(encoding="utf-8")),
                                              clip, info, frames / SAMPLE_RATE)
            except (ValueError, KeyError, TypeError, OSError, wave.Error, EOFError, json.JSONDecodeError):
                raise RuntimeError(f"片段 {clip['id']} 的音频或字幕缓存无效；核对费用与授权后用新的输出目录重新生成，未自动重发") from None
            if bool(data["words"]) != entry.get("timestamps"):
                raise RuntimeError(f"片段 {clip['id']} 的字幕缓存与 manifest 记录不一致；未自动重发")
            cached[fp] = (frames, data, entry.get("timestamps_reason", ""))
    segments, takes_index, missing, pending, last_call = [], {}, [], [], None
    for clip, info, body, take, fp in attempts:
        audio_path, words_path = out / (fp + ".wav"), out / (fp + ".words.json")
        entry = manifest.get(fp, {})
        selected_take = clip.get("selected_take") or 1
        if fp in cached:
            frames, data, reason = cached[fp]
        elif only is not None and clip["id"] not in only:
            if take == selected_take:
                missing.append(clip["id"])
            continue
        else:
            if last_call is not None and time.monotonic() - last_call < min_interval:
                # 官方 T2A RPM：免费 10、付费 20；顺序调用并留间隔，不并发。
                time.sleep(min_interval - (time.monotonic() - last_call))
            request_id = str(uuid.uuid4())
            entry = {"status": "uncertain", "request_id": request_id, "clip": clip["id"], "take": take,
                     "model": body["model"], "voice": body["voice_setting"]["voice_id"], "endpoint": endpoint,
                     "billed_characters_estimate": billed_characters(body["text"])}
            manifest[fp] = entry
            save(manifest_path, manifest)
            started = last_call = time.monotonic()
            try:
                status, trace, payload = call_api(endpoint, body, key, timeout=180)
            except Exception as exc:
                entry.update(error_type=type(exc).__name__, seconds=round(time.monotonic() - started, 3))
                if isinstance(exc, urllib.error.HTTPError):
                    entry["http_status"] = exc.code
                save(manifest_path, manifest)
                raise RuntimeError(f"片段 {clip['id']} take {take} 请求异常（{type(exc).__name__}），结果不确定，未自动重试") from None
            if debug:
                print("response: " + json.dumps(redact(payload, key), ensure_ascii=False), file=sys.stderr)
            base = payload.get("base_resp") if isinstance(payload, dict) else None
            code = base.get("status_code") if isinstance(base, dict) else None
            entry.update(http_status=status, trace_id=trace or (payload.get("trace_id") if isinstance(payload, dict) else None),
                         status_code=code, seconds=round(time.monotonic() - started, 3))
            if code != 0:
                message = str(base.get("status_msg", "")) if isinstance(base, dict) else ""
                entry.update(status="uncertain" if code in UNCERTAIN_CODES or type(code) is not int else "failed",
                             status_msg=message.replace(key, "[REDACTED]")[:500])
                save(manifest_path, manifest)
                hint = {1002: "触发限流，调大 --min-interval 后再试", 1004: "鉴权失败，检查密钥与接入地址是否同一区域",
                        1008: "余额不足", 1026: "输入内容触发审核", 2054: "音色 ID 不存在或账号无权限",
                        2013: "参数错误"}.get(code, "")
                raise RuntimeError(f"片段 {clip['id']} take {take} MiniMax 返回 {code} {message} {hint}".strip())
            data_field = payload.get("data") or {}
            extra = payload.get("extra_info") or {}
            try:
                pcm = decode_wav(bytes.fromhex(data_field.get("audio") or ""))
            except (ValueError, wave.Error, EOFError):
                entry["error_type"] = "invalid_audio"
                save(manifest_path, manifest)
                raise RuntimeError(f"片段 {clip['id']} take {take} 返回成功但音频无效；结果不确定（可能已计费），未自动重试") from None
            frames = len(pcm) // 2
            write_wav(audio_path, pcm)
            del pcm
            reason = ""
            if not body["subtitle_enable"]:
                data, reason = empty_subtitles(clip, info), "disabled"
            elif not data_field.get("subtitle_file"):
                data, reason = empty_subtitles(clip, info), "not_returned"
            else:
                try:
                    data = parse_subtitles(download_subtitles(data_field["subtitle_file"], 60), clip, info,
                                           frames / SAMPLE_RATE)
                    reason = "" if data["words"] else "not_returned"
                except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                    data = empty_subtitles(clip, info)
                    reason = "download_failed" if isinstance(exc, (OSError, urllib.error.URLError)) else "invalid_subtitles"
            save(words_path, data)
            entry.update(status="complete", usage_characters=extra.get("usage_characters"),
                         audio_length_ms=extra.get("audio_length"), words_file=words_path.name,
                         timestamps=bool(data["words"]), timestamps_reason=reason)
            save(manifest_path, manifest)
            cached[fp] = (frames, data, reason)
        if clip.get("takes", 1) > 1:
            alias = out / "takes" / f"{clip['id']}-take{take}.wav"
            alias.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audio_path, alias.with_name(alias.name + ".tmp"))
            alias.with_name(alias.name + ".tmp").replace(alias)
            takes_index.setdefault(clip["id"], []).append({"take": take, "fingerprint": fp,
                                                           "file": str(alias.relative_to(out))})
        if take == selected_take:
            selection = "single" if clip.get("takes", 1) == 1 else (
                "selected" if clip.get("selected_take") is not None else "provisional")
            if selection == "provisional":
                pending.append(clip["id"])
            segments.append({"clip": clip, "frames": frames, "audio": audio_path.name, "words_file": words_path.name,
                             "subtitles": data, "reason": reason, "take": take, "selection": selection})
    save(out / "takes.json", takes_index)
    result = {"clips": len(jobs), "pending_selection": pending}
    if missing:
        # 不交付缺段或残留的旧成品；单段文件仍保留。
        for name in ("final.wav", "final.srt", "timeline.json"):
            (out / name).unlink(missing_ok=True)
        return {**result, "audio": None, "missing_clips": missing,
                "note": "--only 之外有片段缺少缓存；只生成了所选片段，未拼接 final"}
    final = out / "final.wav"
    concatenate(segments, final)
    timeline = build_timeline(segments)
    save(out / "timeline.json", timeline)
    srt = out / "final.srt"
    srt.with_name("final.srt.tmp").write_text(srt_text(timeline), encoding="utf-8")
    srt.with_name("final.srt.tmp").replace(srt)
    return {**result, "audio": str(final.resolve()), "timestamps": timeline["timestamps"],
            "duration": timeline["duration"], "timeline": str((out / "timeline.json").resolve()),
            "srt": str(srt.resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--text-file", type=Path, required=True)
    prep.add_argument("--voice", default=VOICE)
    prep.add_argument("--model", default=MODEL, choices=sorted(MODELS))
    prep.add_argument("--pronunciation-table", type=Path, help="中英发音表 JSON；快照写入计划，含英文稿默认用随附表")
    prep.add_argument("--out", type=Path, required=True)
    for command in ("preview", "render"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--plan", type=Path, required=True)
        cmd.add_argument("--out", type=Path, required=True)
        cmd.add_argument("--voices", type=Path, help="角色→音色 JSON；按角色覆盖计划 voices，clip 显式 voice 优先")
        if command == "render":
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--only", help="逗号分隔的片段 id，只为这些片段发新请求")
            cmd.add_argument("--retry-uncertain", action="store_true")
            cmd.add_argument("--timestamps", action=argparse.BooleanOptionalAction, default=None,
                             help="词级字幕（默认开启）；--no-timestamps 关闭，覆盖计划设置")
            cmd.add_argument("--region", choices=sorted(ENDPOINTS), default="cn",
                             help="cn=api.minimax.cn（默认），bj=api-bj.minimaxi.com（华北）")
            cmd.add_argument("--min-interval", type=float, default=3.5, help="两次请求的最小间隔秒数（RPM 限制）")
            cmd.add_argument("--debug-response", action="store_true", help="stderr 输出脱敏响应，不含密钥、音频与字幕链接")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            table = load_pronunciations(args.pronunciation_table) if args.pronunciation_table else None
            plan = prepare(args.text_file.read_text(encoding="utf-8"), args.voice, args.model, table)
            validate(resolve_voices(plan))
            save(args.out, plan)
            result = {"plan": str(args.out.resolve())}
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            voices = json.loads(args.voices.read_text(encoding="utf-8")) if args.voices else None
            if args.voices and not isinstance(voices, dict):
                raise ValueError("voices 文件须为角色到音色 ID 的对象")
            if args.command == "preview":
                preview(plan, args.out, voices=voices)
                result = {"preview": str((args.out / "director.html").resolve())}
            else:
                only = [s for s in args.only.split(",") if s] if args.only else None
                result = render(plan, args.out, args.dry_run, args.retry_uncertain, args.timestamps, voices, only,
                                ENDPOINTS[args.region], args.min_interval, args.debug_response)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
