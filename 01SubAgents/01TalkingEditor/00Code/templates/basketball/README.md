# 可选：动作旁的短字幕

此模板用于自行审核后的运动/动作视频。文字、时间、位置、字体和颜色均按本条素材选择，不做自动跟踪，不生成真实场景的事实判断，也不执行发布。

先按上级 README 安装引擎。在 `00Code` 中执行：

```sh
.venv/bin/python templates/basketball/render_preview.py "/path/to/source.mp4" \
  --plan "/path/to/caption-plan.json" --output "/path/to/preview-output" --verify
```

将 [合成示例](caption-plan.example.json) 复制到自己的工作目录，根据原音、真实画面、视频尺寸和时长修改。示例文案与位置只用于说明结构，不代表已审核的素材。原片需有音轨；SDR 当前接受明确标注的 BT.709，HDR 可按输入标签转换为 SDR。

每条 `cue` 必须包含 2–8 字短句、时间、非空的真实审核 `evidence`，以及 `position: {"x": 0.5, "y": 0.5}`。`x/y` 为整句字幕中心在显示画面中的 0–1 坐标，必须给整句留出安全边距；可选 `end_position` 生成持续本句时长的线性移动。`accent` 和 `style` 控制可选强调及外观。字体需在自己电脑安装；没有指定时按系统选默认字体。

模板会导出 `preview.mp4`、`captions.ass`、`captions.srt` 和渲染记录。画面保持完整时间线及原速，音频以流复制保留。`--verify` 核对每帧时间戳、音频包、完整解码、原片指纹和时长，通过后才保存预览。`--verify-only` 可复验现有预览，覆盖派生文件需显式 `--overwrite`。

`--tonemap-desat` 控制 HDR 高光去饱和，默认沿用媒体引擎的技术初值 2；根据自己的素材预览后调整。源素材编码不支持复制到 MP4、没有音轨或验收不通过时会报错。字幕遮挡人、球、动作轨迹及主观色彩效果仍需逐帧抽查。SRT 不包含坐标与样式，ASS/视频保留。
