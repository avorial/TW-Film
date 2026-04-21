@echo off
cd /d "%~dp0"
git add scraper.py
git commit -m "Fix email download, Walker filter, MSP multi-date, Fandango junk"
git push
pause
