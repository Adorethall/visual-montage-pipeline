# Visual Montage Pipeline

多视频、同品类、音乐驱动的画面向广告混剪工具。首期支持彩妆和美食：发现视觉高光，按BGM节拍编排，加入产品开屏、产品录屏、连续两句口播、Logo、尾贴和CTA，并产出MP4、剪映计划和可编辑标题封面。

## 安装

```bash
pixi install
cp .env.example .env
```

配置 `.env` 中的 Hatchet、Gemma及剪映路径。远程媒体能力通过 `src/worker_stubs` 中的Hatchet任务契约调用。

## 当前Beauty全流程

当前 `beauty.csv` 有15条启用素材。执行一次完整Beauty流程并输出1个剪映工程：

```bash
pixi run visual-montage batch-run \
  --manifest data/inputs/manifests/beauty.csv \
  --category beauty \
  --limit 15 \
  --count 1 \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --profile profiles/categories/beauty.yaml \
  --asset-library data/assets/asset-library.yaml \
  --registry data/catalog/candidate-registry.sqlite \
  --voiceover-mode regenerate \
  --force-audio \
  --run-id beauty-full-v1
```

完整执行链路：

```text
读取Manifest
→ 视频分析缓存检查
→ FFmpeg低成本代理
→ 短视频Gemma全片审阅
→ 长视频Marlin召回 + Gemma复核
→ 高光候选登记与跨批次去重
→ 素材音频抽取、YAMNet和BGM入库
→ 自动选择未重复且适合Beauty节奏的BGM
→ 生成N套低重复剪辑方案
→ 产品开屏、录屏、尾贴和Logo包装
→ OmniVoice生成连续口播，失败回退VoxCPM
→ MOSS ASR + 真实气口检测生成多段字幕
→ 口播期间BGM自动降低
→ 封面抽帧、标题和PNG Logo
→ 输出并验证剪映工程
→ 成功后提交候选与BGM使用记录
```

常用控制参数：

```text
--count 5                 输出5套低重复工程
--force-analysis          忽略视频分析缓存
--force-audio             只重跑素材音频分析
--cache-only              禁止远程分析，只允许使用缓存
--voiceover-mode cached   优先复用TTS缓存
--voiceover-mode regenerate 强制重生成TTS
--draft-batch-folder week_0720 按周标记剪映草稿，并生成周归档文件夹
--music-analysis FILE     不自动选歌，改用指定BGM分析文件
--voiceover-audio FILE    不生成TTS，改用指定口播文件
```

## 输入与调用

复制并编辑 `data/inputs/manifests/materials.example.csv` 和 `data/inputs/campaigns/beauty_20s.example.yaml`。Manifest视频必须使用绝对路径；Campaign必须声明产品开屏、录屏、尾贴和一条至少包含两句话的连续口播。

```bash
pixi run validate-config

visual-montage validate-input \
  --manifest data/inputs/manifests/materials.csv \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --cover-title "今天想换哪种妆感"
```

生产阶段为 `analyze → analyze-music → compose → package → cover → preview → render → jianying → validate`。每阶段读写 `data/runs/{run_id}`，按文件哈希、模型、Prompt和Profile版本缓存，失败后从最近完成阶段续跑。

当前确定性阶段可独立调用：

```bash
visual-montage analyze-music --music bgm.mp3 --output data/runs/demo/music/music-analysis.json

visual-montage compose \
  --candidate-pool data/runs/demo/analysis/candidate-pool.json \
  --profile profiles/categories/beauty.yaml \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --music-analysis data/runs/demo/music/music-analysis.json \
  --output data/runs/demo/plans/ad-package-plan.json

visual-montage cover-metadata \
  --title "今天想换哪种妆感" --frame cover-clean.jpg \
  --video-id beauty_001 --timestamp 12.4 \
  --output data/runs/demo/cover/cover.json
```

## 广告结构

```text
0.0–0.8   视觉Hook
0.8–7.2   第一段高光
7.2–8.7   产品开屏
8.7–12.8  产品录屏
12.8–17.5 第二段高光
17.5–20.0 尾贴和CTA
```

口播从产品开屏开始，连续覆盖录屏，最多延伸到第二段高光3秒。音频只生成
一条连续文件，字幕拆成多条原生可编辑文字段。口播期间BGM按Campaign配置
降低音量，口播结束后恢复，并保持音乐源时间连续。

Beauty当前配置：

```text
口播开始：7.2秒
BGM ducking：-7 dB
口播区间BGM音量：约0.4467
```

## 封面

系统从最终高光中选择主体清晰、有张力且有标题空间的一帧，标题优先从当前
Profile的 `batch_generation.cover_titles` 选择，然后将原始帧和精确标题交给
GPT Image 2完成封面排版。提交前先把注册表中的准确PNG Logo合成到
`cover-image2-input.png`，提示词要求锁定Logo并保留左上品牌安全区。成功时输出
`cover-image2.png`，剪映直接使用这张带标题和Logo的封面，不再重复添加
`CoverTitle`或封面Logo层。`cover-clean.jpg` 始终保留，Image2失败且Campaign
设置 `fail_open: true` 时，会回退到本地白字黑影封面。

Image2使用Rings CLI认证，不需要在项目 `.env` 重复填写密钥。首次使用前运行：

```bash
rings auth login
```

Campaign配置：

```yaml
cover:
  editable_title: false
  image2:
    enabled: true
    task_key: gpt-image2
    size: 1080x1920
    quality: high
    request_timeout_seconds: 1800
    fail_open: true
```

生成结果按“原始帧内容 + 标题 + Image2配置”缓存到
`data/cache/covers/image2/`；相同封面输入再次运行时不会重复调用Image2。

## Logo包装顺序

默认包装顺序为：封面阶段使用静态PNG；封面结束后，第一个可展示Logo的
高光片段使用同色动态MOV；后续高光片段继续使用静态PNG。产品开屏、产品
录屏和尾贴继续按照 `hide_on_clip_types` 隐藏Logo。

白字和黑字均通过 `data/assets/asset-library.yaml` 注册。切换颜色时修改品类
默认项即可，不需要修改导出代码：

```yaml
category_defaults:
  beauty:
    logo_asset_id: rednote_logo_black_vertical
    animated_logo_asset_id: rednote_logo_black_vertical_animated
    cover_logo_asset_id: rednote_logo_black_vertical
```

新计划可显式提供 `cover_logo_overlay`、`logo_intro_overlay` 和
`logo_overlay`。旧计划如果只有原来的PNG Logo，剪映导出器会按文件名或
Asset ID 在注册表中查找同色动态Logo，自动套用上述顺序。

## 新增品类

普通画面向品类不修改核心Python：

1. 新增 `profiles/categories/{category}.yaml`，声明事件、权重、降权项、镜头长度和封面事件。
2. 新增 `prompts/marlin-events/{category}.yaml`，配置3–6组事件查询及strict/normal/broad变体。
3. 新增Gemma复核规则和封面标题策略。
4. 新增Campaign默认文案与连续口播模板。
5. 在Asset Library绑定品类产品录屏及其必需开屏。
6. 增加测试夹具，用5–20条代表素材校准。

只有分屏、画中画、舞蹈同步等新剪辑形态才需要扩展代码。

## Marlin结果不好时

Marlin只负责候选召回：Gemma复核事件真实性；过宽窗口通过密集抽帧和场景边界收窄；候选不足时依次执行broad查询、Gemma完整视频发现和场景关键帧发现；重复结果按时间、感知哈希和来源合并。每个查询组记录验证Precision，连续低质量则降权或禁用。系统宁可返回 `partial`，也不使用错误镜头凑数。

人工反馈写入 `data/feedback/{category}/feedback.jsonl`，用于调整查询和权重。

## 同品类批量生成与跨批次去重

视频分析阶段会把全部有效候选写入
`data/catalog/candidate-registry.sqlite`。候选ID由品类、视频ID和标准化时间段
生成；重复分析同一时间段会更新原记录，不会创建一份新的历史资产。

导入分析功能上线前已经生成的候选池：

```bash
pixi run visual-montage candidate-register \
  --candidate-pool data/runs/beauty-analysis-5/candidate-pool.json \
  --category beauty
```

一次生成5套低重复方案：

```bash
pixi run visual-montage batch-compose \
  --candidate-pool data/runs/beauty-analysis/candidate-pool.json \
  --profile profiles/categories/beauty.yaml \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --music-analysis data/runs/beauty-first/music/music-analysis.json \
  --registry data/catalog/candidate-registry.sqlite \
  --output-dir data/runs/beauty-batch-001 \
  --run-id beauty-batch-001 \
  --count 5
```

批量选中的候选先标记为 `reserved`，所以在下一批选择中不会再次使用。只有成片
和剪映工程成功后才提交：

```bash
pixi run visual-montage candidate-finalize \
  --run-id beauty-batch-001 \
  --state committed
```

如果渲染或交付失败，应释放预占：

```bash
pixi run visual-montage candidate-finalize \
  --run-id beauty-batch-001 \
  --state released
```

每批输出 `diversity-report.json`，包含候选重复率、来源重复率、首镜头是否重复、
未使用候选占比和每条方案的完整路径。产品开屏、录屏、Logo和尾贴不计入高光
重复率。候选不足时方案标记为 `partial`，不会用弱镜头强行填满。

## 视频分析缓存

默认分析会根据源视频指纹、品类配置、Gemma提示词、模型ID、Marlin查询配置、
代理参数和分析流程版本查询 SQLite 缓存。完全匹配时跳过 FFmpeg 代理、Marlin
和 Gemma，直接复用候选；当前 Run 仍会重新输出 raw JSON、候选池和 contact
sheet。

忽略缓存并强制重新分析：

```bash
pixi run python scripts/analyze_visual_batch.py ... --force
```

只允许读取缓存，缓存缺失时立即失败，不产生远程调用：

```bash
pixi run python scripts/analyze_visual_batch.py ... --cache-only
```

视频文件、profile、提示词、模型、Marlin查询或代理设置任一发生变化，都会生成
新的缓存键并重新分析。

## 一键批量生成剪映草稿

`batch-run` 串联分析缓存、候选登记、跨批次去重、广告包装、封面和剪映导出。
不传口播音频时自动生成或复用TTS缓存；同一批草稿复用一份连续口播，不会为
每个草稿重复调用TTS。

视频分析同时提取素材音轨。检测到音乐后，系统使用YAMNet识别音乐、说话、
歌唱和Rap，按需调用Audio Separator分离人声与伴奏，并对人声Stem执行ASR。
带歌词歌曲和Rap允许进入BGM库；音乐上叠加持续口播时只允许使用分离后的伴奏；
纯口播、环境声和判断不明确的音频不会自动入选。

不传 `--music-analysis` 时，`batch-run` 从 SQLite BGM库按音乐分、Beauty BPM
匹配、口播风险、音频指纹和历史使用次数自动选歌：

```bash
pixi run visual-montage batch-run \
  --manifest data/inputs/manifests/beauty.csv \
  --category beauty \
  --limit 20 \
  --count 5 \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --profile profiles/categories/beauty.yaml \
  --voiceover-audio data/runs/beauty-first/voiceover/product-sequence.wav \
  --asset-library data/assets/asset-library.yaml \
  --registry data/catalog/candidate-registry.sqlite \
  --run-id beauty-20-5-v1
```

需要人工指定音乐时，额外添加：

```bash
--music-analysis data/runs/beauty-first/music/music-analysis.json
```

每个验证通过的方案会生成独立剪映草稿、`jianying-plan.json`、
`jianying-result.json`、`cover-clean.jpg`、`cover-preview.jpg` 和
`cover.json`。草稿成功且无跳过素材时才提交候选使用记录；候选不足的方案标记
为 `partial` 并释放预占，导出失败的方案也自动释放。

测试缓存和整条剪映链路但禁止远程分析时，添加：

```bash
--cache-only
```

只重跑素材音频分析、保留视频画面缓存时使用：

```bash
--force-audio
```

Audio Separator 首次失败或超时后会在当前批次熔断，后续素材使用YAMNet音乐+
对话双预设保守判断，不会让20条视频反复等待同一个远程故障。Separator恢复后，
系统会继续使用人声Stem、伴奏Stem和ASR区分口播与歌词。

## 口播生成与强制重生成

默认优先使用文案、声线、语速和模型配置对应的TTS缓存。单独生成口播：

```bash
pixi run visual-montage generate-voiceover \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --output data/runs/beauty-voice/voiceover/product-sequence.wav
```

忽略缓存并强制重新调用 OmniVoice；失败时自动回退 VoxCPM：

```bash
pixi run visual-montage generate-voiceover \
  --campaign data/inputs/campaigns/beauty_20s.yaml \
  --output data/runs/beauty-voice/voiceover/product-sequence.wav \
  --force
```

`batch-run` 不传 `--voiceover-audio` 时会自动生成或复用缓存。要求本批强制
重新生成时使用：

```bash
--voiceover-mode regenerate
```

生成结果写入同目录的 `voiceover-result.json`，包含实际provider、模型ID、时长、
缓存命中状态和OmniVoice/VoxCPM尝试记录。

## MOSS字幕时间戳与气口对齐

TTS生成或载入口播后，系统调用：

```text
OpenMOSS-Team/MOSS-Transcribe-Diarize
```

环境变量：

```env
MOSS_ASR_API_BASE=https://api.ten-rings.adtensor.com/llm/v1
MOSS_ASR_ENDPOINT=/audio/transcriptions
MOSS_ASR_API_KEY=
MOSS_ASR_MODEL=OpenMOSS-Team/MOSS-Transcribe-Diarize
MOSS_ASR_RESPONSE_FORMAT=text
```

字幕对齐顺序：

```text
MOSS ASR获取句段时间戳和说话人
→ Campaign原文修正产品名和转写文本
→ 按句号、逗号和语义短句拆分
→ FFmpeg检测真实静音和气口
→ 为每个理想切点选择最近气口
→ 无内部气口时按字符发音权重降级
→ 第一条强制从TTS 0秒开始
→ 最后一条强制在TTS实际时长结束
```

每次生成：

```text
voiceover-result.json  TTS模型、Provider、时长和缓存状态
subtitles.json         MOSS原始结果、气口、标准化字幕和校验
product-sequence.wav   一条连续口播音频
```

字幕强校验：

```text
字幕总时长误差 <= 50ms
字幕之间不得重叠
第一条字幕开始 = 0
最后一条字幕结束 = WAV实际时长
每条字幕在剪映中保持独立可编辑
```

## 测试

```bash
pixi run test
```

普通测试不调用付费模型；远程Stub和模型测试单独标记为integration。
