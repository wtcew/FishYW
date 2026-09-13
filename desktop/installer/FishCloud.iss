; FishCloud 智能运维平台 · 安装版向导脚本（Inno Setup 6）
; 页面顺序：欢迎 → 许可条款（必选接受）→ 选择安装位置 → 附加任务 → 安装 → 完成
; 编译：cd desktop/installer && "C:\...\Inno Setup 6\ISCC.exe" FishCloud.iss
; 产物：release/installer/FishCloud-Setup-1.0.0.exe（内含免安装版全部内容：便携 Python + 应用 + 启动器）
;
; 前置：release/免安装版/ 已含最新构建（App/ + FishCloud.exe + 启动/停止FishCloud.bat）；
;       重打包前先同步最新源码与前端产物，见 desktop/make_release.py。
;
; 安全约定（2026-09-13）：**绝不打包 App\.env** —— 它含真实模型密钥、JWT 密钥与
; Webhook 令牌。发布包只带 App\.env.example，用户按需复制成 .env。

#define MyAppName "FishCloud 智能运维平台"
#define MyAppVersion "1.0.0"
#define MyAppExeName "FishCloud.exe"
#define PortableDir "..\..\release\免安装版"

[Setup]
AppId={{7E9B2C64-5A31-4E8D-9F02-B6A1C4D8E3F5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=FishCloud
AppPublisherURL=https://github.com/wtcew/FishYW
AppSupportURL=https://github.com/wtcew/FishYW
DefaultDirName={autopf}\FishCloud
DirExistsWarning=no
DefaultGroupName=FishCloud
LicenseFile=license-terms.txt
OutputDir=..\..\release\installer
OutputBaseFilename=FishCloud-Setup-1.0.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
PrivilegesRequired=lowest
SetupIconFile=..\..\desktop\icon\fishcloud.ico
UninstallDisplayIcon={app}\App\desktop\icon\fishcloud.ico
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "quicklaunchicon"; Description: "创建快速启动栏快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#PortableDir}\App\*"; DestDir: "{app}\App"; Excludes: ".env,.env.local"; Flags: recursesubdirs ignoreversion createallsubdirs
Source: "{#PortableDir}\FishCloud.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PortableDir}\启动FishCloud.bat"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#PortableDir}\停止FishCloud.bat"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\App\desktop\icon\fishcloud.ico"
Name: "{group}\停止 FishCloud（结束残留进程）"; Filename: "{app}\停止FishCloud.bat"; IconFilename: "{app}\App\desktop\icon\fishcloud.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\App\desktop\icon\fishcloud.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\App\desktop\icon\fishcloud.ico"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 不自动删除用户数据（数据库/知识库/模型缓存均在项目外 D 盘），仅卸载程序文件
