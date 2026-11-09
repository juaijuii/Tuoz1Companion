@echo off
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --console --clean --name Tuoz1Companion --icon tuoz1.ico --add-data "tuoz1.ico;." tuoz1_companion.py
echo.
echo 打包完成: dist\Tuoz1Companion.exe
pause
