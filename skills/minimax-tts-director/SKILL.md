---
name: minimax-tts-director
description: 把稿件变成有情绪、有节奏的配音成品。基于 MiniMax 同步语音合成（speech-2.8），支持中文、英文及中英混读：按语境校对多音字、姓氏与轻声，用行内拼音按位置纠音（同一段里“华老栓”与“华丽”可以各读各的），用发音字典和 IPA 固定词组、品牌与缩写读法；为旁白和不同角色安排音色、情绪、语速、音量、语调、停顿、笑声叹气等语气词和声音效果器。先看带注音的阅读稿，再按需生成 WAV 音频、SRT 字幕和映射回原文的词级时间戳。适合视频旁白、产品讲解、有声故事和分角色对白，还能打开本地网页让用户录音或上传素材，复刻自己的声音后直接用于配音。
---

# MiniMax TTS 导演

你负责导演和中文校对；脚本负责候选扫描、阅读稿、请求和音频，不另调规划或审稿模型。保留用户原文、指定音色与角色；英文品牌、型号和技术词保留原文，不自动翻译或音译，不改写数字或添加台词。

**MiniMax 没有自然语言语音指令和引用上文。**不要写“温柔地说”之类的指令字段（脚本会拒绝未知字段）。可用的导演手段只有：`emotion`、`speed / vol / pitch`、`marks`（语气词标签与停顿）、`voice_modify`（音高、强度、音色、音效）、音色选择、分段与段后停顿、多 take。想要的效果超出这些时，如实告诉用户。

## 实际流程

需要可执行本地命令的环境、Python 3.10+，唯一依赖 pypinyin。推荐 `uv run scripts/tts.py`（自动装依赖）；没有 uv 时先 `pip install pypinyin==0.55.0` 再 `python scripts/tts.py`。脚本路径从本 skill 目录解析，作品存到用户项目目录。

1. `prepare --text-file 原稿.txt --out plan.json [--voice 音色ID] [--model speech-2.8-hd]` 得到草稿。按自然段或角色调整 `clips`，不要无故逐句切碎；单段本地上限 1000 字，超长明确分段，不截断原文。多角色写顶层 `voices`（角色→音色），片段写 `role`。
2. **校对多音字。**扫描结果全部是 `pending`，来自词典建议，不代表语境校对完成，常见错误包括姓氏和同段同字异读（如“单先生”的“单”、“还要/还款”）。检查全部候选以及姓氏、地名、专名、古文、轻声和“一／不”的变调；用词组标注替换单字，例如 `{"start": 2, "text": "行长", "pinyin": ["hang2", "zhang3"]}`。`start` 是段内从 0 开始的 Python 字符下标，`text` 必须精确匹配原文。判断过的标 `reviewed`；私人姓名等无法确定的读音不能猜，要问用户。每项必须写 `delivery`：
   - `native`：只做展示，让模型自然读。常用词、轻声助词（的、了、得）默认用它。
   - `inline`：**按位置**把该字替换成括号拼音提交，如 `(hua4)老栓`。用于姓氏、专名、模型读错的位置，以及同一段里同字异读。
   - `dictionary`：生成 `pronunciation_dict` 规则 `行长/(hang2)(zhang3)`，**服务端对整段所有该词生效**。用于明确的词组。
   不要因为有多音字就全部强制纠音，强制规则会破坏自然语流；出现真实错读再局部改。详见 [纠音](references/usage.md#纠音三种投递方式)。
3. 按需添加 `marks`：`{"at": 5, "tag": "laughs"}` 在原文第 5 个字前插入语气词（仅 speech-2.8），`{"at": 5, "pause": 0.4}` 插入 0.4 秒停顿（须位于两段可发音文字之间）。原文 `text` 里不要手写 `(laughs)`、`<#0.5#>` 或括号拼音，脚本会拒绝。
4. `preview --plan plan.json --out 阅读稿目录` 生成 `director.html` 与 `director.md`。**向用户展示阅读稿**：正文注音、校对状态、处理方式、标记、导演参数、**实际提交文本**和计费字符。不需要密钥，也不联网。
5. 用户只要审稿或选音色时，到这里就可以停。要生成时，先 `render --plan plan.json --out 音频目录 --dry-run`，说明调用次数、计费字符和估算费用，再按授权执行 `render`（去掉 `--dry-run`）。密钥只从运行时 `MINIMAX_API_KEY` 读取，不打印、不存储。默认接入 `api.minimax.cn`，华北用 `--region bj`。
6. 交付 `final.wav`、`final.srt`、`timeline.json`、每段 `<指纹>.words.json`、阅读稿与 manifest 状态。有试听工具才能说听过；结构检查不能证明读音或演绎正确。用户反馈后只改受影响的段：相同请求会复用本地缓存，只改 `pause_after_ms` 时不会重新请求。

## 导演参数

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `model` | 顶层或片段；默认 `speech-2.8-hd` | `speech-2.8-turbo` 更便宜；语气词标签只有 2.8 支持 |
| `voice` / `voices` | 系统音色、复刻音色或文生音色 ID | 片段显式 `voice` > `--voices` 文件 > 计划 `voices`；常用音色见 [usage](references/usage.md#音色) |
| `emotion` | happy / sad / angry / fearful / disgusted / surprised / calm；2.6 另有 fluent / whisper | 省略时模型按文本自动匹配，通常不必指定 |
| `speed` / `vol` / `pitch` | 0.5～2 / (0,10] / -12～12 整数 | 默认 1 / 1 / 0 |
| `marks` | `{at, tag}` 或 `{at, pause}`，可附 `note` | 标签：laughs chuckle coughs clear-throat groans breath pant inhale exhale gasps sniffs sighs snorts burps lip-smacking humming hissing emm sneezes |
| `voice_modify` | `pitch / intensity / timbre`（-100～100）、`sound_effects` | 音效：spacious_echo、auditorium_echo、lofi_telephone（电话）、robotic |
| `language_boost` | 如 `Chinese`、`English`、`Chinese,Yue`（粤语）、`auto` | 粤语音色必须设 `Chinese,Yue` |
| `text_normalization_mode` | `basic` / `quality` | 数字、日期、金额规范化；quality 用 LLM，仅 2.8/2.6 |
| `takes` / `selected_take` | 1～4 / null 或 1～takes | 每个 take 单独计费；不自动挑选，`null` 时暂用 take 1，标为 provisional，试听后写入 |
| `pause_after_ms` | 0～30000 | 本地拼接的段后静音 |
| `note` | 字符串 | 导演备注，只在阅读稿里显示，不发送 |

强烈情绪、笑和叹气：先选合适的音色，再加对应的 `marks` 标签，必要时再加 `emotion`。**原文里的“嘻嘻嘻”“哈哈”这类象声词会被当成字念出来**，和标签叠在一起会很怪。所以有标签时，要征得用户同意后删掉象声词；如果原稿必须保留象声词，就不加标签。`happy` 偏欢快，不适合阴冷的冷笑，拿不准时不指定情绪。效果要靠试听，可以开 `takes: 2～3` 让用户挑。

## 中英混读与发音表

含英文的稿件，`prepare` 会把 [pronunciations.json](references/pronunciations.json) 的快照写入计划顶层 `pronunciation_table`；`--pronunciation-table` 可以换成自定义表。`words` 的值写 IPA（不带括号，例如 `"Anthropic": "ænˈθrɒpɪk"`）会编译成 `Anthropic/(ænˈθrɒpɪk)`，写 `null` 表示保留原生读法；`acronyms` 是逐字母转写（`API/A P I`）；`models` 是型号转写，`decimal_point: "点"` 时 `5.5` 读“五点五”。规则按词界编译，**区分大小写**（实测 `api` 不会命中 `API`）。脚本会拒绝误改子串（如 `API` 命中 `OpenAPI`）、多条规则命中位置重叠，以及覆盖已校对多音字的规则。格式见 [usage](references/usage.md#发音表格式)。

## 时间戳与字幕

默认开启词级字幕（`subtitle_type: word`），`--no-timestamps` 或 `enable_subtitle: false` 可以关闭。服务端字幕按**提交文本**定位，脚本把它映射回**原文**：行内拼音 `(hua4)` 的时间记在原字“华”上，字典词组（“华丽”）是一个词条，语气词时间单独放在 `tags`，停顿标记不进字幕。英文会被切成子词片段（如 H / ai / k / u）。被纠音的词条带 `pronounce`，即服务端实际采用的注音（如 `ㄏㄨㄚˋ`）。渲染后可以用它核对行内拼音和字典规则是否被服务端采用，但它不能代替试听。SRT 用服务端分段（每段不超过 50 字），`timeline.json` 的时间已经加上了拼接偏移。字幕下载失败或格式异常时，照常交付音频，并在 manifest 记录 `timestamps_reason`，不会自动重发。

## 声音复刻

用户想复刻自己或某人的声音时，不要让用户在对话里贴音频，用本地网页完成：

1. 先说明三件事：复刻需要账号完成实名认证；音色费 9.9 元在**第一次正式合成**时扣，试听按语音合成单价计费；复刻后 7 天内没有正式使用会被删除。只能复刻本人或已获授权的声音。
2. 在后台运行 `uv run scripts/clone.py serve --library <用户项目>/voices/clones.json --open`，把第一行输出里的 `url` 发给用户。录音、上传、命名、试听和授权确认都由用户在页面上完成，复刻按钮也由用户自己点。
3. 用户在页面上点“满意，就用它”或“结束”后，进程退出。关闭页面约 3 分钟后也会自动退出，输出里的 `ended` 记录了退出原因。退出后读取输出里的 `cloned` 或音色库，把 voice_id 写进计划的 `voices`（例如 `{"旁白": "MyVoice_2026"}`）。第一次用这个音色 render 前，dry-run 说明里要提醒会触发 9.9 元音色费。
4. 音色库目录里有用户的原声素材，提醒用户不要提交到公开仓库。删除音色只在用户明确要求时由用户在页面上操作。

接口、字段、错误码和音色库格式见 [声音复刻](references/usage.md#声音复刻)。

## 调用、计费与安全

- 计费字符按实际提交文本计算：汉字 ×2，其他字符（含标点、空格、标记）×1。dry-run 给出精确字符数和 speech-2.8 系列的估算费用（hd 3.5 元/万字符、turbo 2 元/万字符，以控制台为准）。
- 请求顺序发送，默认间隔 3.5 秒（付费账号 RPM 20，免费账号 10，免费账号要加 `--min-interval 6.5`）。不并发，不自动重试。
- 服务端明确返回的错误码（音色不存在、参数错误、限流、余额不足等）记为 `failed`，修正后重新 render 即可。网络异常、超时和 1000/1001 记为 `uncertain`（可能已计费），默认拒绝重发；要先核对控制台记录并取得授权，才能加 `--retry-uncertain`。
- `--only 001,003` 只为指定片段发新请求，用于局部试听；其他片段没有缓存时不生成 `final.wav`，避免交付残缺或过期的成品。
- 同一个输出目录不要同时运行两个 render。

详细字段、示例与实测记录见 [运行与计划格式](references/usage.md)。
