# Visual Montage Pipeline

多视频、同品类、音乐驱动的画面向广告混剪工具。首期支持彩妆和美食：发现视觉高光，按BGM节拍编排，加入产品开屏、产品录屏、连续两句口播、Logo、尾贴和CTA，并产出MP4、剪映计划和可编辑标题封面。

## 安装

```bash
pixi install
cp .env.example .env
```

配置 `.env` 中的 Hatchet、Gemma及剪映路径。远程媒体能力通过 `src/worker_stubs` 中的Hatchet任务契约调用。

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

口播从产品开屏开始，连续覆盖录屏，最多延伸到第二段高光3秒。音频一次生成，字幕按句拆分，BGM执行一次连续ducking。

## 封面

系统从完整素材和最终高光中选择主体清晰、有张力且有标题空间的一帧，输出 `cover-clean.jpg`、`cover-preview.jpg` 和 `cover.json`。剪映计划使用底图和原生文字段，标题保持可编辑；中文标题限制6–16字、最多两行，并必须由视频主题支持。

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

## 测试

```bash
pixi run test
```

普通测试不调用付费模型；远程Stub和模型测试单独标记为integration。
