@echo off
:: 获取当前时间戳
for /f "delims=" %%i in ('powershell -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"') do set datetime=%%i

:: 强制提示用户输入提交信息
set /p commit_msg="Please enter your commit info (press enter to confirm): "

:: 如果用户什么都没输入，设置一个默认文本
if "%commit_msg%"=="" set commit_msg=Commit At %datetime%

git add .
:: 将用户的输入和时间戳组合在一起
git commit -m "%commit_msg%"
git push origin main