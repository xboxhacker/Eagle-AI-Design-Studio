@echo off
setlocal
cd /d "%~dp0"
title Eagle AI Design Studio launcher
set "LOG=%~dp0launcher.log"
echo ==== %date% %time% ==== > "%LOG%"

rem ---- find a REAL python (the Microsoft Store "python" alias is a stub that exits silently)
set "PY="
rem tested versions first: a newer Python may not have PySide6 wheels yet
py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if defined PY goto have_py
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if defined PY goto have_py
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if defined PY goto have_py
python -c "import sys" >nul 2>nul && set "PY=python"
if defined PY goto have_py
echo.
echo  Python was not found.
echo  Install Python 3.13 from https://www.python.org/downloads/windows/
echo  and tick "Add python.exe to PATH" on the first installer screen.
echo  (If you have the Microsoft Store "python" alias, turn it off in
echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases.)
goto fail

:have_py
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo Using %PY% (Python %PYVER%)
echo python %PYVER% via %PY% >> "%LOG%"

rem ---- create the virtual environment once
if exist ".venv\Scripts\python.exe" goto have_venv
echo Creating virtual environment...
%PY% -m venv .venv >> "%LOG%" 2>&1
if errorlevel 1 (
  echo  Could not create .venv - see launcher.log
  goto fail
)

:have_venv
rem ---- (re)install packages when requirements.txt differs from the last successful install
if not exist ".venv\installed.req" goto install
fc /b requirements.txt ".venv\installed.req" >nul 2>nul
if not errorlevel 1 goto run
:install
echo Installing packages (first run or after an update)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG%" 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo  Package install FAILED. Scroll up for the pip error.
  echo  Python %PYVER% may be too new for PySide6 - Python 3.13 or 3.12 is the safe choice.
  goto fail
)
copy /y requirements.txt ".venv\installed.req" >nul

:run
echo Starting Eagle AI Design Studio...
".venv\Scripts\python.exe" run.py
if errorlevel 1 (
  echo.
  echo  Eagle AI Design Studio exited with an error - details above and in crash.log
  goto fail
)
exit /b 0

:fail
echo.
echo  Copy the text above (plus crash.log if it exists) into a GitHub issue to report the problem.
pause
exit /b 1
