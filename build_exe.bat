@echo off
setlocal
cd /d "%~dp0"

set "VPYTHON=venv\Scripts\python.exe"

if exist "%VPYTHON%" goto :venv_ok

echo == Creating venv ==
call :find_python
if errorlevel 1 exit /b 1
call %PYBASIS% -m venv venv
if errorlevel 1 (
    echo [ERROR] venv creation failed.
    exit /b 1
)
"%VPYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
echo == Installing requirements ==
"%VPYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] requirements install failed.
    exit /b 1
)

:venv_ok
if exist "NMSPakInspector.exe" (
    for %%F in ("NMSPakInspector.exe") do echo Current root exe timestamp: %%~tF
)

"%VPYTHON%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo == Installing requirements ==
    "%VPYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] requirements install failed.
        exit /b 1
    )
)

"%VPYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo == Installing PyInstaller ==
    "%VPYTHON%" -m pip install pyinstaller==6.22.2
    if errorlevel 1 (
        echo [ERROR] PyInstaller install failed.
        exit /b 1
    )
)

call :trim_pyside6

echo == Building NMSPakInspector.exe ==
"%VPYTHON%" -m PyInstaller --noconfirm --clean NMSPakInspector.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

if not exist "dist\NMSPakInspector.exe" (
    echo [ERROR] Build did not produce dist\NMSPakInspector.exe
    exit /b 1
)

echo == Copying to project root... ==
copy /Y "dist\NMSPakInspector.exe" "NMSPakInspector.exe" >nul
if errorlevel 1 (
    echo [ERROR] Copy to project root failed.
    exit /b 1
)

for %%F in ("NMSPakInspector.exe") do echo Rebuilt root exe timestamp: %%~tF

echo == Cleaning build artifacts... ==
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist __pycache__ rmdir /s /q __pycache__

echo Done. NMSPakInspector.exe rebuilt at project root.
exit /b 0

:trim_pyside6
set "P6DIR=venv\Lib\site-packages\PySide6"
if not exist "%P6DIR%" exit /b 0
"%VPYTHON%" -c "import pathlib;p=pathlib.Path('venv/Lib/site-packages/PySide6');keep={'Qt6Core.dll','Qt6Gui.dll','Qt6Widgets.dll','Qt6Network.dll','Qt6Svg.dll'};[f.unlink() for f in p.glob('Qt6*.dll') if f.name not in keep]"
if exist "%P6DIR%\opengl32sw.dll" del /q "%P6DIR%\opengl32sw.dll"
"%VPYTHON%" -c "import pathlib;p=pathlib.Path('venv/Lib/site-packages/PySide6');keep={'QtCore','QtGui','QtWidgets','QtNetwork'};[f.unlink() for f in list(p.glob('Qt*.pyd'))+list(p.glob('Qt*.pyi')) if f.stem not in keep]"
if exist "%P6DIR%\qml" rmdir /s /q "%P6DIR%\qml"
if exist "%P6DIR%\resources" rmdir /s /q "%P6DIR%\resources"
if exist "%P6DIR%\metatypes" rmdir /s /q "%P6DIR%\metatypes"
if exist "%P6DIR%\translations" rmdir /s /q "%P6DIR%\translations"
exit /b 0

:find_python
if defined PYTHON if exist "%PYTHON%" (
    set "PYBASIS="%PYTHON%""
    exit /b 0
)
py -V >nul 2>&1
if not errorlevel 1 (
    set "PYBASIS=py"
    exit /b 0
)
where python >nul 2>&1
if not errorlevel 1 (
    set "PYBASIS=python"
    exit /b 0
)
echo [ERROR] No Python 3 interpreter found. Install Python 3.10+ and re-run.
exit /b 1
