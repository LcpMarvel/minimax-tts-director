# 运行与计划格式

在 skill 目录执行（其他目录用脚本绝对路径）：

```sh
uv run scripts/tts.py prepare --text-file 原稿.txt --out plan.json
uv run scripts/tts.py preview --plan plan.json --out 阅读稿
uv run scripts/tts.py render --plan plan.json --out 音频 --dry-run
# 仅在用户授权合成后；运行时环境提供 MINIMAX_API_KEY
uv run scripts/tts.py render --plan plan.json --out 音频
```

`prepare` 不改动原文，按句末标点打包成不超过 500 字的片段；生成的汉字多音字候选全部为 `pending`、`delivery: native`。`preview` 允许待校对项；`render`（包括 dry-run）要求每个扫描候选都被一条 `reviewed` 标注覆盖。不要批量把 pending 改成 reviewed：先读语境。

## 计划格式

计划只有一个来源，就是这份 JSON。未知字段直接报错，所以 `direction`、`reference_text` 这类火山字段要删掉。

```json
{
  "title": "白骨精现身",
  "model": "speech-2.8-hd",
  "voices": {"旁白": "male-qn-jingying", "白骨精": "wumei_yujie"},
  "enable_subtitle": true,
  "language_boost": "Chinese",
  "text_normalization_mode": "basic",
  "pronunciation_table": null,
  "clips": [{
    "id": "bgj-laugh",
    "role": "白骨精",
    "text": "听说吃上唐僧一块肉，就能长生不老。",
    "speed": 1, "vol": 1, "pitch": 0,
    "voice_modify": {"timbre": -20},
    "pause_after_ms": 400,
    "takes": 2, "selected_take": null,
    "note": "先笑一声，再得意地低声说",
    "marks": [{"at": 0, "tag": "laughs"}, {"at": 2, "pause": 0.3}],
    "readings": [
      {"start": 12, "text": "长", "pinyin": ["chang2"], "status": "reviewed", "delivery": "native", "note": "长生：长久义"}
    ],
    "pronunciation_dict": {"tone": ["唐僧/(tang2)(seng1)"]}
  }]
}
```

- 顶层 `model`、`language_boost`、`text_normalization_mode`、`enable_subtitle` 可以被片段覆盖。`pronunciation_table` 只放顶层。
- `id` 必须唯一，只能用字母、数字、下划线、短横线，1～64 个字符。
- 拼音写数字声调，轻声写 `5`，ü 写 `v`（如 `lv4`）。阅读稿会自动转成声调符号。
- 标注可以覆盖单字或词组（最多 16 字），每个字一个拼音，不能重叠。用词组标注时，要删掉被它覆盖的单字标注。

## 纠音：三种投递方式

先按语境确定目标读音，再选投递方式。阅读稿里的注音只用于审稿，不会自动发给服务端。

| delivery | 实际请求 | 何时用 |
| --- | --- | --- |
| `native` | 原字不变 | 常用读音、轻声助词；默认选它，避免过度纠音 |
| `inline` | 该位置每个字替换成 `(拼音)`，如 `(hua4)老栓` | 姓氏、专名、模型读错的位置、同一段里同字异读；**只影响这个位置** |
| `dictionary` | `pronunciation_dict.tone` 里加 `词/(p1)(p2)` | 明确的词组；**整段所有该词都生效** |

示例：“华老栓忽然坐起身，披上那件华丽的旧袍子。”里，姓氏“华”读 hua4，“华丽”读 hua2。字典按词全局生效，分不开这两处，所以姓氏用 `inline`，见 [examples/hua-laoshuan](../../../examples/hua-laoshuan/plan.json)。

脚本的保守检查（全部在联网前完成）：

- 同一原词有两种字典读音时报错，提示改用 `inline`。
- 字典规则命中的位置如果已有不同的校对读音，报错。
- 文本转写规则（如 `银行/银杭`）覆盖已校对多音字时，报错。
- 不同规则命中位置重叠时，报错（官方没说明重叠时谁优先，所以不猜）。
- 规则命中行内拼音或插入标记时，报错。

`pronunciation_dict.tone` 可以手写补充规则：`词/(p1)(p2)` 是拼音；`郑栅洁/(zheng4)(shan1)杰` 是拼音与汉字混写；`omg/oh my god` 是文本转写；`live/(lɪv)` 是 IPA。补充规则不会自动把候选标为已校对。

### 轻声与自然语流

普通轻声助词保留 `native`，阅读稿里照样标 `de5`、`le5`。只有真实错读或用户明确要求时，才局部纠音。用户反馈语流不连贯时，先撤掉相关强制规则做对照试听，不要不断叠加规则。

## marks：语气词与停顿

`at` 是插入位置：在原文第 `at` 个字之前插入，`at = len(text)` 表示插在末尾。同一位置的多个标记按数组顺序插入。

- `{"at": n, "tag": "laughs"}` 提交为 `(laughs)`，仅 speech-2.8-hd / speech-2.8-turbo 支持。可选标签：laughs 笑声、chuckle 轻笑、coughs 咳嗽、clear-throat 清嗓子、groans 呻吟、breath 换气、pant 喘气、inhale 吸气、exhale 呼气、gasps 倒吸气、sniffs 吸鼻子、sighs 叹气、snorts 喷鼻息、burps 打嗝、lip-smacking 咂嘴、humming 哼唱、hissing 嘶嘶声、emm 嗯、sneezes 喷嚏。
- `{"at": n, "pause": 0.5}` 提交为 `<#0.5#>`。范围 0.01～99.99 秒，最多两位小数。必须位于两段可发音文字之间，同一位置不能放两个停顿。片段之间的停顿用 `pause_after_ms`。
- 标记不能落在已标注词组的内部。
- **笑声、叹气等交给标签，原文里不要再写象声词。**稿件里的“嘻嘻嘻”“哈哈哈”“唉”会被当成字念出来；如果和标签叠在一起，听起来就是“一声笑 + 念出来的 xī xī xī”（2026-11-09 实测，用户反馈“笑得怪”）。如果台词必须保留象声词，就不要再加标签，交给模型自己演。改动原稿前先征得用户同意。
- 句首的标签后面不能紧跟停顿：停顿要求前后都有可发音文字，而标签不算。笑完想停一下，可以把停顿放到第一个短语之后，或把笑声单独放在上一段末尾，再用 `pause_after_ms` 控制间隔。

## 音色

`voice` 可以是系统音色、复刻音色或文生音色 ID。脚本只检查格式和占位值，不能证明账号有权限；音色不存在时服务端返回 2054。完整列表见 [系统音色列表](https://platform.minimax.cn/docs/faq/system-voice-id.md) 或查询可用音色 API。部分中文普通话音色：

| ID | 名称 | ID | 名称 |
| --- | --- | --- | --- |
| male-qn-jingying | 精英青年 | female-chengshu | 成熟女性 |
| male-qn-qingse | 青涩青年 | female-tianmei | 甜美女性 |
| male-qn-badao | 霸道青年 | female-yujie | 御姐 |
| male-qn-daxuesheng | 青年大学生 | female-shaonv | 少女 |
| Chinese (Mandarin)_Gentleman | 温润男声 | Chinese (Mandarin)_News_Anchor | 新闻女声 |
| Chinese (Mandarin)_Reliable_Executive | 沉稳高管 | Chinese (Mandarin)_Lyrical_Voice | 抒情男声 |
| Chinese (Mandarin)_Humorous_Elder | 搞笑大爷 | Chinese (Mandarin)_Kind-hearted_Antie | 热心大婶 |
| clever_boy | 聪明男童 | lovely_girl | 萌萌女童 |
| wumei_yujie | 妩媚御姐 | lengdan_xiongzhang | 冷淡学长 |

复刻音色 ID 由用户在复刻时自定义（8～256 位，字母开头），直接填进 `voice` 即可。按官方说明，复刻音色 7 天内没有正式调用会被删除，首次使用时按音色收费。复刻流程见 [声音复刻](#声音复刻)。

`preview / render --voices FILE` 读取裸映射对象（`{"旁白": "音色ID"}`），按角色覆盖计划里的 `voices`。片段里显式写的 `voice` 优先级最高。

## 发音表格式

```json
{
  "version": 1,
  "words": {"Anthropic": "ænˈθrɒpɪk", "Claude": "klɔːd", "DeepSeek": null},
  "acronyms": {"API": "A P I", "S-1": "S one"},
  "models": ["5.5", "V4.1"],
  "decimal_point": "点"
}
```

五个字段都必需。`words` 的值是 IPA，不含括号、斜杠和空白，`null` 表示原生读法。`acronyms` 的原词须为大写英文、数字或短横线，目标须为英文。`models` 是点分数字型号，可带 V 前缀；`decimal_point` 设为 `点` 时读“5点5”（服务端再规范化为“五点五”），设为 `point` 时读 “five point five”。同一词只能出现在三类中的一类。只有正文按词界实际出现的词才会编译成规则；未出现的词不进请求，也不影响缓存。不要把所有数字都写进 models：如果同一个 `5.5` 既是型号又是价格，全局字典分不开，要删掉该规则或拆段。

## 请求与响应（2026-11-09 实测）

请求体示意：

```json
{
  "model": "speech-2.8-hd",
  "text": "(hua4)老栓说：<#0.5#>(laughs)这件华丽的衣服，用 API 处理。",
  "stream": false,
  "voice_setting": {"voice_id": "male-qn-jingying", "speed": 1, "vol": 1, "pitch": 0},
  "audio_setting": {"sample_rate": 24000, "format": "wav", "channel": 1},
  "subtitle_enable": true, "subtitle_type": "word", "output_format": "hex",
  "pronunciation_dict": {"tone": ["API/A P I"]}
}
```

实测结论（共 4 次调用，未保存凭据或链接）：

- 成功和错误都返回 HTTP 200，看 `base_resp.status_code`：0 表示成功，2054 表示音色不存在。
- `format: wav` 返回标准 RIFF 头的 24 kHz、16-bit、单声道 WAV（hex 编码），`voice_modify` 可用。`pcm` 是同规格的裸数据，但官方说明效果器不支持 pcm，所以统一用 wav。
- `subtitle_file` 是对象存储的预签名链接，内容是分段数组：`text / text_begin / text_end / time_begin / time_end`（毫秒浮点）和 `timestamped_words[] {word, word_begin, word_end, time_begin, time_end}`。位置是**提交文本的码点下标**；`<#x#>` 停顿不出现在字幕里；`(hua4)` 按注音符号拆成多条，`word` 相同；字典词组（`华丽`）同样拆成多条，`word` 为原词；`(laughs)` 作为一个词出现；英文切成子词片段。
- 计费 `usage_characters` = 提交文本中汉字 ×2 + 其他字符 ×1（含括号拼音和标记），两次调用（55、67）均与本地计算一致。
- 发音字典区分大小写：`API/A P I` 不会命中 `api`。IPA 规则生效（`pronounce_text` 显示 IPA）。
- 字幕的 `pronounce_word` 是服务端采用的注音：行内 `(hua4)` 为 `ㄏㄨㄚˋ`，字典 `华丽/(hua2)(li4)` 为 `ㄏㄨㄚˊㄌㄧˋ`，`(lv4)(nv3)` 为 `ㄌㄩˋ / ㄋㄩˇ`，因此 ü 写 `v` 有效。脚本把它存为词条的 `pronounce` 字段（与原字相同时省略）。这能证明服务端采用了规则，不能证明音频听感正确。
- 同日端到端验收（6 次调用）：用 render 跑 [hua-laoshuan](../../../examples/hua-laoshuan/plan.json)、[dialogue](../../../examples/dialogue/plan.json)（旁白 + 白骨精 2 take）和 ü 短句，全部成功，`usage_characters` 与本地估算逐条一致，字幕均映射回原文，`chuckle` 时间记入 `tags`，停顿 0.4 秒体现在句间间隔。
- 字典、行内拼音、语气词与情绪的实际听感本次没有做听审，正式使用前须试听。

## 输出目录

- `requests.json`：每段的请求体，不含密钥。dry-run 也会写。
- `manifest.json`：按请求指纹记录 `status`（uncertain / failed / complete）、`trace_id`、`status_code`、`usage_characters`、`audio_length_ms`、耗时与字幕状态。调用前先写入 uncertain，成功后改为 complete。
- `<指纹>.wav`、`<指纹>.words.json`：每个请求的音频和字幕。`words.json` 里有 `text`（原文）、`submitted_text`、`words[] {word, start, end, startTime, endTime, submitted?, pronounce?}`（秒，相对该段起点，`start/end` 为原文下标，`pronounce` 为服务端注音）、`sentences[]` 和 `tags[]`。
- `takes/<id>-take<k>.wav`、`takes.json`：多 take 的试听文件。
- `final.wav`、`timeline.json`、`final.srt`：成品。timeline 里的时间已加上片段偏移，偏移按采样数累计，包含所有段后停顿。

指纹由完整请求体决定（take 2 以后还会加上 take 编号），所以修改文本、读音、标记、音色或参数，只会让受影响的段重新请求；`pause_after_ms` 和 `selected_take` 不进请求体，修改它们只会重新拼接。字幕开关也在请求体里。缓存损坏时，脚本在发出任何新请求前就报错，不会静默重新计费。

`timestamps_reason` 的取值：空（正常）、`disabled`、`not_returned`、`download_failed`、`invalid_subtitles`（位置与提交文本不一致或时间无效）。字幕时间精度由服务端负责，本工具不做强制对齐，不裁剪音频，也不修正时间。

## 声音复刻

`scripts/clone.py` 只用 Python 标准库。它会启动一个本地网页，让用户自己录音或上传素材、完成复刻：

```sh
uv run scripts/clone.py serve --library 项目/voices/clones.json --open   # 打印 {"url", "library"}，阻塞到用户点“完成”
uv run scripts/clone.py list --library 项目/voices/clones.json [--account] # 本地音色库；--account 只读查询账号内复刻音色
```

- **网页流程**：
  1. 录音或上传素材。录音在浏览器里转成 24kHz 单声道 WAV；上传支持 mp3/m4a/wav，时长 10 秒～5 分钟，≤20MB。
  2. 填写 `voice_id` 和显示名。
  3. 按需打开降噪、音量归一化、语种增强，或添加小于 8 秒的示例音频及其逐字文本。
  4. 按需填写试听文本（≤1000 字）并选择模型。
  5. 勾选授权确认后复刻。
  
  页面会显示费用和 7 天删除规则，并能播放试听、复制 ID，以及删除音色（需要再次输入 voice_id）。
- **服务端调用**：`POST /v1/files/upload`（`purpose=voice_clone`，示例音频用 `prompt_audio`）→ `POST /v1/voice_clone`；账号查询走 `/v1/get_voice`，删除走 `/v1/delete_voice`。密钥只在本地进程内使用，不会发到浏览器。
- **网页安全**：只监听 `127.0.0.1`，端口随机，URL 带一次性 token，还会校验 Host 头（防 DNS 重绑定）。所有接口都要 token，`/api/audio` 只能读音色库音频目录里的文件。
- **音色库**：一个 JSON 文件（`{"voices": [...]}`），素材、示例音频和试听音频存在同目录的 `<库名>-audio/` 下。条目字段有 `voice_id`、`label`、`status`（cloned / uncertain / deleted）、`created_at`、`delete_if_unused_after`、`source`、`prompt`、`preview`（文本、模型、计费字符、本地音频）、`options`、`file_id`、`trace_id`。素材是用户本人的声音，**不要提交到公开仓库**。
- **失败处理**：服务端明确返回错误码时，不写入音色库，页面会提示原因，例如 2038 需要实名认证、2039 voice_id 已存在、2037 时长不符。复刻请求遇到网络异常时，记为 `uncertain`，要先到控制台核对。
- **计费**：复刻音色费 9.9 元，在第一次用该音色正式合成时扣；试听按语音合成单价计费。复刻后 7 天内没有正式使用，音色会被删除。`get_voice` 只列出正式使用过的复刻音色。删除后的 voice_id 不能再用。
- **页面结束**：用户点“满意，就用它”或“结束”时，进程退出。页面每 20 秒发一次心跳：关闭页面后约 3 分钟（`--idle-timeout`）自动退出，一直没人打开时 15 分钟（`--startup-timeout`）后退出。复刻请求进行中不会退出。退出时打印 `{"library", "ended", "cloned", "voices"}`，`ended` 取值为 `finished` / `page_closed` / `never_opened`。

## 本地自检

在源码仓库根目录执行（安装后的 skill 不含测试）：

```sh
uv run --with pypinyin==0.55.0 python tests/selfcheck.py
python3 tests/clonecheck.py   # 声音复刻（标准库，模拟接口）
```

协议依据：[MiniMax 同步语音合成 HTTP](https://platform.minimax.cn/docs/api-reference/speech-t2a-http.md)、[接入地址与文本归一化](https://platform.minimax.cn/docs/guides/speech-t2a-websocket.md)、[定价](https://platform.minimax.cn/docs/pricing/overview.md)、[速率限制](https://platform.minimax.cn/docs/guides/rate-limits.md)、[错误码](https://platform.minimax.cn/docs/api-reference/errorcode.md)，以及开放平台的文件上传、快速复刻、查询与删除音色接口文档，均于 2026-11-09 核对。复刻流程已于 2026-11-10 真实跑通 2 次：试听 `demo_audio` 是 mp3 链接；试听文本里的 `(laughs)`、`(chuckle)` 标签和 `<#0.4#>` 停顿在 speech-2.8-hd 下生效（用户试听确认笑声是真笑）。
