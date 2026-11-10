@echo off
cd /d "%~dp0"
rem 在一个干净的环境里打包：只装打包工具和本机认字要用的两个库（numpy、只用 CPU 的 onnxruntime）。
rem 不要直接用系统 Python 打——系统里要是装了带显卡加速的 onnxruntime-gpu，会把几百 MB 的显卡文件一起打进去。
if not exist build\venv\Scripts\python.exe python -m venv build\venv
build\venv\Scripts\python.exe -m pip install --upgrade pip pyinstaller numpy onnxruntime
build\venv\Scripts\python.exe -m PyInstaller --onefile --console --clean --name Tuoz1Companion --icon tuoz1.ico --add-data "tuoz1.ico;." --add-data "assets/ocr/ch_PP-OCRv4_rec_infer.onnx;assets/ocr" --collect-binaries onnxruntime tuoz1_companion.py
echo.
dist\Tuoz1Companion.exe --ocr-selftest
echo.
echo 打包完成: dist\Tuoz1Companion.exe
pause
