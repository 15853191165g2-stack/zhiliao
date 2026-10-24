; ---------------------------------------------------------------------------
; 知了 · 电脑版 安装程序（NSIS 3.x，Unicode）
;
; 设计决策，改了要先想清楚为什么：
;  1. 按用户安装（RequestExecutionLevel user）→ 不弹 UAC，装到
;     %LOCALAPPDATA%\Programs\ZhiLiao。为什么不装 Program Files：知了的第一件事
;     就是建库写 265 条通知，装在 Program Files 里普通用户没写权限，装完必坏。
;  2. 静默卸载（/S）**默认保留数据** —— 没人看着的时候绝不删用户数据。
;  3. 卸载若选择保留数据，会把 quanzhi.db 搬到 %LOCALAPPDATA%\ZhiLiaoData，
;     重装时自动问「要不要恢复上次的数据」。
; ---------------------------------------------------------------------------

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!define APP_NAME     "知了"
!define APP_TITLE    "知了 · 电脑版"
!define APP_VERSION  "1.0.0"
!define APP_PUBLISHER "知了（连小理大赛参赛作品）"
!define APP_URL      "https://github.com/15853191165g2-stack/zhiliao"
!define REG_UNINST   "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZhiLiao"
!define DATA_DIR     "$LOCALAPPDATA\ZhiLiaoData"

Name "${APP_TITLE} ${APP_VERSION}"
Caption "${APP_TITLE} 安装程序"
OutFile "知了-Setup-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\ZhiLiao"
InstallDirRegKey HKCU "Software\ZhiLiao" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
BrandingText "${APP_TITLE} ${APP_VERSION}"

VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName"     "${APP_TITLE}"
VIAddVersionKey "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey "FileVersion"     "${APP_VERSION}.0"
VIAddVersionKey "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "MIT License"
VIAddVersionKey "FileDescription" "${APP_TITLE} 安装程序"
VIAddVersionKey "Comments"        "只开一个窗口，看全学校所有通知"

; ---- 界面 ----
!define MUI_ICON   "zhiliao.ico"
!define MUI_UNICON "zhiliao.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "安装 知了 · 电脑版"
!define MUI_WELCOMEPAGE_TEXT "知了把学校十几个部门的通知页面合成一条信息流，$\r$\n每条都配 3 行摘要和重要度，重要的事还能读给你听。$\r$\n$\r$\n点「下一步」开始。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "$INSTDIR\quanzhi_desktop.py"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动知了"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "license.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Var KeepData
Var RestoreData

; ---------------------------------------------------------------------------
; 安装前检查
; ---------------------------------------------------------------------------
Function .onInit
  ${IfNot} ${AtLeastWin7}
    MessageBox MB_ICONSTOP "知了需要 Windows 7 或更高版本。" /SD IDOK
    Abort
  ${EndIf}

  ; 64 位检查：自带的 Python 解释器是 64 位的，32 位系统跑不起来
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "知了需要 64 位 Windows（安装包内含 64 位 Python 运行时）。" /SD IDOK
    Abort
  ${EndIf}

  ; 已经在运行就别装 —— 文件被占用时复制会失败，报错还不好懂
  nsExec::ExecToStack 'cmd /C netstat -ano -p TCP | findstr /R ":8765[ ]" | findstr "LISTENING"'
  Pop $0
  Pop $1
  ${If} $0 == 0
    retry_port:
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
      "检测到 8765 端口正在被占用，知了可能已经在运行。$\r$\n$\r$\n请先关闭知了（右键任务栏图标退出），再点「重试」。" \
      /SD IDCANCEL IDRETRY retry_port
    Abort
  ${EndIf}

  ; 上次卸载时保留过数据？
  StrCpy $RestoreData 0
  ${If} ${FileExists} "${DATA_DIR}\quanzhi.db"
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "发现上次卸载时保留的通知数据。$\r$\n$\r$\n要恢复到这次安装里吗？（推荐：保留已抓的 265 条通知）" \
      /SD IDYES IDYES restore IDNO skip_restore
    restore:
      StrCpy $RestoreData 1
    skip_restore:
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------------------
; 组件
; ---------------------------------------------------------------------------
Section "知了 主程序（必需）" SecMain
  SectionIn RO
  SetOverwrite on
  SetOutPath "$INSTDIR"

  ; 95MB / 4000+ 文件，进度条按真实大小算
  AddSize 97280

  File /r "staging\*.*"
  File "zhiliao.ico"
  File "使用说明.txt"

  ; 上次保留的数据搬回来（覆盖安装包里的初始库）
  ${If} $RestoreData == 1
    CopyFiles /SILENT "${DATA_DIR}\quanzhi.db" "$INSTDIR\backend\quanzhi.db"
    ${If} ${FileExists} "${DATA_DIR}\desktop.json"
      CreateDirectory "$INSTDIR\state"
      CopyFiles /SILENT "${DATA_DIR}\desktop.json" "$INSTDIR\state\desktop.json"
    ${EndIf}
  ${EndIf}

  ; 给桌面壳用的：窗口图标
  CreateDirectory "$INSTDIR\state"

  ; 开始菜单
  CreateDirectory "$SMPROGRAMS\知了"
  CreateShortCut "$SMPROGRAMS\知了\知了.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\quanzhi_desktop.py"' \
                 "$INSTDIR\zhiliao.ico" 0 SW_SHOWNORMAL "" "$INSTDIR"
  CreateShortCut "$SMPROGRAMS\知了\使用说明.lnk" "$INSTDIR\使用说明.txt"
  CreateShortCut "$SMPROGRAMS\知了\卸载知了.lnk" "$INSTDIR\Uninstall.exe"

  ; 注册表：控制面板「应用和功能」里能看到
  WriteRegStr HKCU "Software\ZhiLiao" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\ZhiLiao" "Version" "${APP_VERSION}"
  WriteRegStr HKCU "${REG_UNINST}" "DisplayName"     "${APP_TITLE}"
  WriteRegStr HKCU "${REG_UNINST}" "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr HKCU "${REG_UNINST}" "DisplayIcon"     "$INSTDIR\zhiliao.ico"
  WriteRegStr HKCU "${REG_UNINST}" "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr HKCU "${REG_UNINST}" "URLInfoAbout"    "${APP_URL}"
  WriteRegStr HKCU "${REG_UNINST}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${REG_UNINST}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${REG_UNINST}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${REG_UNINST}" "NoModify" 1
  WriteRegDWORD HKCU "${REG_UNINST}" "NoRepair" 1

  ; 让「应用和功能」显示正确体积
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${REG_UNINST}" "EstimatedSize" "$0"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "桌面快捷方式" SecDesktop
  CreateShortCut "$DESKTOP\知了.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\quanzhi_desktop.py"' \
                 "$INSTDIR\zhiliao.ico" 0 SW_SHOWNORMAL "" "$INSTDIR"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}    "知了主体：自带 Python 运行时，不依赖本机是否装过 Python。约 95 MB。"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "在桌面放一个「知了」图标，双击即用。"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ---------------------------------------------------------------------------
; 卸载
; ---------------------------------------------------------------------------
Section "Uninstall"
  ; 静默卸载默认保留数据 —— 没人看着的时候不删用户数据
  StrCpy $KeepData 1
  ${IfNot} ${Silent}
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "卸载前请确认知了已经关闭。$\r$\n$\r$\n是否保留已抓取的通知数据（含已读/置顶状态）？$\r$\n保留后重新安装可以接着用；选「否」将彻底删除。" \
      /SD IDYES IDYES keep_data IDNO drop_data
    drop_data:
      StrCpy $KeepData 0
    keep_data:
  ${EndIf}

  ${If} $KeepData == 1
    CreateDirectory "${DATA_DIR}"
    ${If} ${FileExists} "$INSTDIR\backend\quanzhi.db"
      CopyFiles /SILENT "$INSTDIR\backend\quanzhi.db" "${DATA_DIR}\quanzhi.db"
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\state\desktop.json"
      CopyFiles /SILENT "$INSTDIR\state\desktop.json" "${DATA_DIR}\desktop.json"
    ${EndIf}
  ${EndIf}

  Delete "$DESKTOP\知了.lnk"
  Delete "$SMPROGRAMS\知了\知了.lnk"
  Delete "$SMPROGRAMS\知了\使用说明.lnk"
  Delete "$SMPROGRAMS\知了\卸载知了.lnk"
  RMDir "$SMPROGRAMS\知了"

  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\backend"
  RMDir /r "$INSTDIR\state"
  RMDir /r "$INSTDIR\logs"
  Delete "$INSTDIR\quanzhi_desktop.py"
  Delete "$INSTDIR\知了桌面版.py"
  Delete "$INSTDIR\知了.bat"
  Delete "$INSTDIR\自检.bat"
  Delete "$INSTDIR\zhiliao.ico"
  Delete "$INSTDIR\使用说明.txt"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\LICENSE"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${REG_UNINST}"
  DeleteRegKey HKCU "Software\ZhiLiao"
SectionEnd
