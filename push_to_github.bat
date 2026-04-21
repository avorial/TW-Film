@echo off
echo.
echo ============================================================
echo  TW-Film — Push to GitHub
echo  Target: https://github.com/avorial/TW-Film
echo ============================================================
echo.

cd /d "%~dp0"

:: Fix Windows 260-character path length limit
git config --global core.longpaths true

:: Clean up any broken previous attempt
if exist ".git" (
    echo Cleaning up previous git attempt...
    rmdir /s /q .git
)

:: Initialize git
echo Initializing git repo...
git init
git branch -M main

:: Set remote (safe to run even if it already exists)
git remote remove origin 2>nul
git remote add origin https://avorial@github.com/avorial/TW-Film.git

:: Stage all files
echo Staging files...
git add .

:: Commit
git commit -m "v3.11: add Picturegoer Film Club"

:: Push
echo Pushing to GitHub...
git push -u origin main --force

echo.
echo Done! View at: https://github.com/avorial/TW-Film
pause
