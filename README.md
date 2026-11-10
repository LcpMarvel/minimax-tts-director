# minimax-tts-director · MiniMax TTS 导演

把你的稿件变成有情绪、有节奏的配音成品。让 AI Agent 为中文、英文及中英混读稿件校对读音，给旁白和不同角色安排音色、情绪、语气词与停顿，再通过 MiniMax 同步语音合成（speech-2.8）生成配音、SRT 字幕和映射回原文的词级时间戳。

这是 [volce-tts-director](https://github.com/LcpMarvel/volce-tts-director) 的 MiniMax 版本，工作流相同：先看带注音的阅读稿，再听实际音频。

## 和火山版的区别

| 能力 | 火山版 | MiniMax 版 |
| --- | --- | --- |
| 同一段里同字异读（“华老栓”与“华丽”） | 字典按词生效，需要可听别名替字 + 指令 | **行内拼音按位置纠音** `(hua4)老栓`，不替字、不用指令 |
| 英文品牌读法 | 自然语言提示，非确定性 | **发音字典直接写 IPA** `Anthropic/(ænˈθrɒpɪk)` |
| 情绪与演绎 | 自然语言语音指令、引用上文、perform 模式 | `emotion` 枚举 + 语气词标签（笑、叹气、喘气等 19 种）+ 文本内停顿 + 声音效果器；**没有**自然语言指令和引用上文 |
| 字幕 | 服务端字词时间 | 服务端词级时间映射回原文，并附服务端实际采用的注音，可用来核对纠音 |
| 声音复刻 | 只能用已有复刻音色 | **本地网页录音/上传 → 一键复刻**，结果写入音色库，voice_id 直接用于配音 |
| 计费 | 按字符 / 按时长 | 按字符（汉字 ×2），dry-run 能给出精确字符数和估算费用 |

## 安装

使用 [vercel-labs/skills](https://github.com/vercel-labs/skills)，在准备使用 skill 的项目目录执行：

```sh
# Claude Code
npx skills add LcpMarvel/minimax-tts-director --skill minimax-tts-director --agent claude-code -y
# Codex
npx skills add LcpMarvel/minimax-tts-director --skill minimax-tts-director --agent codex -y
```

需要 Python 3.10+，推荐 [uv](https://docs.astral.sh/uv/)（`uv run` 会自动安装 `pypinyin`）。正式合成需要运行环境提供 `MINIMAX_API_KEY`（[接口密钥](https://platform.minimax.cn/user-center/basic-information/interface-key)）；校对、预览和 dry-run 不需要密钥。

## 开始使用

把原稿交给 Agent，并说明希望的音色和演绎，例如：

```text
用 minimax-tts-director 给这段稿子配音：旁白用精英青年，白骨精用妩媚御姐，
冷笑那句多出两个 take 给我挑。先给我看带注音的阅读稿，暂不合成。
```

Agent 会：

1. 用 `prepare` 生成计划，结合语境校对全部多音字、姓氏、专名和轻声，并为每处选择纠音方式：自然读法、行内拼音或发音字典。
2. 安排音色、情绪、语速、语气词、停顿和多 take。
3. 用 `preview` 生成阅读稿，展示注音、处理方式、标记、导演参数、**实际提交文本**和计费字符。
4. 按你的授权先跑 `render --dry-run` 说明费用，再生成 `final.wav`、`final.srt`、`timeline.json`。

## 命令

```sh
cd skills/minimax-tts-director
uv run scripts/tts.py prepare --text-file 原稿.txt --out plan.json
uv run scripts/tts.py preview --plan plan.json --out 阅读稿
uv run scripts/tts.py render --plan plan.json --out 音频 --dry-run
uv run scripts/tts.py render --plan plan.json --out 音频            # 会收费
uv run scripts/tts.py render --plan plan.json --out 音频 --only 002  # 只重做某段
```

复刻声音（本地网页，用户在页面上录音或上传并确认，点“完成”后返回）：

```sh
uv run scripts/clone.py serve --library 项目/voices/clones.json --open
uv run scripts/clone.py list --library 项目/voices/clones.json
```

计划格式、纠音规则、标记、音色、发音表和输出文件说明见 [usage.md](skills/minimax-tts-director/references/usage.md)；智能体工作流见 [SKILL.md](skills/minimax-tts-director/SKILL.md)。

## 示例

- [中文多音字](examples/chinese/plan.json)：银行、行长、重庆、音乐，“单先生”的“单”用行内拼音，“还要 / 还款”按词组区分。
- [同字异读](examples/hua-laoshuan/plan.json)：姓氏“华”读 huà，“华丽”读 huá。
- [中英混读](examples/mixed/plan.json)：Anthropic / Claude / Haiku 用 IPA，型号 5.5 读“五点五”。
- [分角色对白](examples/dialogue/plan.json)：旁白 + 白骨精，`(laughs)` 笑声标签（原文不写“嘻嘻嘻”）、2 个 take。

每个示例目录里都有 `director.html` / `director.md` 阅读稿。

## 验证状态

2026-11-09 共做了 10 次真实调用（探测协议 4 次，端到端验收 6 次），确认了 WAV 格式、字幕结构与位置单位、计费字符公式、字典大小写敏感、IPA、ü 写 v，以及错误码的形态，详见 [usage.md](skills/minimax-tts-director/references/usage.md#请求与响应2026-11-09-实测)。**这些调用都没有做听审。**行内拼音、字典、语气词和情绪的实际听感，需要正式使用时试听确认。

离线自检：

```sh
uv run --with pypinyin==0.55.0 python tests/selfcheck.py
python3 tests/clonecheck.py
```

声音复刻的网页和接口流程，已用模拟接口在浏览器里走通：上传、复刻、试听播放、删除确认、完成退出。2026-11-10 做了 2 次真实复刻，文件上传、复刻、试听音频（mp3 链接）下载都已跑通，试听里的笑声标签也生效。
