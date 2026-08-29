@echo off
setlocal
set PROFILE_DIR=%~dp0browser_profile
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

echo ======================================================================
echo Starting Google Chrome with Remote Debugging Port 9222...
echo Profile Directory: %PROFILE_DIR%
echo ======================================================================

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%" --no-first-run --no-default-browser-check "https://chatgpt.com/c/6a918785-fca8-83ee-8bb6-422f629090d0" "https://notebook.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3"
    echo Chrome launched successfully on port 9222!
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%" --no-first-run --no-default-browser-check "https://chatgpt.com/c/6a918785-fca8-83ee-8bb6-422f629090d0" "https://notebook.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3"
    echo Chrome launched successfully on port 9222!
) else (
    echo Google Chrome not found in standard installation paths.
)
endlocal
