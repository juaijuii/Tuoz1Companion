# 文字识别模型

`ch_PP-OCRv4_rec_infer.onnx` 是 PaddleOCR 的 PP-OCRv4 手机版文字识别模型（中英文），
取自 PyPI 上的 `rapidocr-onnxruntime` 1.4.4 安装包（RapidAI/RapidOCR 转成 ONNX 的版本），Apache-2.0 许可。
字表存在模型文件自己的元数据里。

插件用它在本机认海克斯三选一卡片标题上的名字（v1.9.0 起）。打包时会和 `numpy`、`onnxruntime` 一起放进 exe。
机器人仓库里放着同一个文件，本机认不了的时候由机器人来认。

SHA-256：`48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b`
