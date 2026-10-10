# V2 案例配方

- `timeline.json`：223个动作单元，完整整数帧时间线、源入出点、裁切、镜像、变速支点、原声参数与终检修正。
- `sources.json`：43个源文件的ID、文件名、时长和媒体元数据，已去掉机器绝对路径。
- `drum_events.json`：本例音乐的450个优选瞬态证据。频段类别为启发判断，非确定乐器识别。
- `case_report.json`：原工作站V2的检查结果，不能当作公共移植脚本的新运行结果。

需自行提供相同发行版动画和指定音乐。`prepare`递归按文件名唯一匹配，重名或缺失会停止并说明。匹配文件名不证明内容/时间码相同；发行版有变化时重新检查源动作与时间范围。

本例用歌曲开头的210.1秒，保留打斗原声，输出1920×1080、30fps。源画面按1920×1080规范坐标裁切到有效画面再加上下黑边。新任务不应自动沿用这些数值。

在仓库根目录运行：

```bash
python action-match-amv/scripts/amv.py prepare \
  --project work/v2 --recipe action-match-amv/examples/v2 \
  --media-root '/path/to/anime' --music '/path/to/Jax - La La Land.mp3'
python action-match-amv/scripts/amv.py check --project work/v2
```

先用 `render --shots 0,34,80-82,133,173-174 --project work/v2` 检查关键片段，再渲染全片、混音、组装和验证。完整命令与字段见 [CLI说明](../../scripts/CLI.md)。

时间线已经包含最终人工修正，不再需要原工作站的 `build_timeline.py` 和 `refine_final.py`。新素材的动作排序与锚点仍需重新分析，此配方不会自动替用户选片。
