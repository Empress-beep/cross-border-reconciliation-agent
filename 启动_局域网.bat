@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   跨境对账系统 v2.0 - 局域网模式（waitress）
echo   其他同事访问: http://本机IP:8765
echo   查看本机IP: ipconfig
echo   首次使用若无法访问，请在防火墙放行 8765 端口
echo ============================================
waitress-serve --host=0.0.0.0 --port=8765 app:app
pause
