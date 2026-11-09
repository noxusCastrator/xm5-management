; Compile through tools/build_installer.py; PayloadFiles is a generated exact list.
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef PayloadFiles
  #error PayloadFiles is required
#endif
#ifndef ChineseMessages
  #error ChineseMessages is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
#ifndef OutputName
  #define OutputName "XM5Studio-0.2.2-Windows-x64-Setup"
#endif

[Setup]
AppId={{167851CF-0701-4B99-8FE4-AC2EB066C7D8}
AppName=XM5 Studio
AppVersion=0.2.2
AppVerName=XM5 Studio 0.2.2
AppPublisher=XM5 Studio contributors
VersionInfoVersion=0.2.2.0
DefaultDirName={localappdata}\Programs\XM5 Studio
DefaultGroupName=XM5 Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
DisableWelcomePage=no
DisableProgramGroupPage=yes
DisableDirPage=no
AllowRootDirectory=no
AllowNetworkDrive=no
AllowUNCPath=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
UsePreviousLanguage=no
ShowLanguageDialog=yes
LanguageDetectionMethod=uilanguage
CloseApplications=no
RestartApplications=no
AppMutex=Global\XM5Studio.Application.167851CF-0701-4B99-8FE4-AC2EB066C7D8
SetupMutex=Global\XM5Studio.Setup.167851CF-0701-4B99-8FE4-AC2EB066C7D8
UninstallDisplayName=XM5 Studio
UninstallDisplayIcon={app}\XM5Studio.exe
SetupIconFile={#PayloadDir}\xm5lib\ui\assets\studio\app.ico
OutputDir={#OutputPath}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
SetupLogging=yes
TimeStampsInUTC=yes
TouchDate=none
TouchTime=none

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "{#ChineseMessages}"

[CustomMessages]
english.Welcome=Manage local music, playlists and PEQ presets, and prepare music for the ONIX XM5.%n%nThis installs XM5 Studio for your Windows account. Music folders are chosen when you open the application.
chinesesimplified.Welcome=管理本地音乐、歌单和 PEQ 预设，为 ONIX XM5 准备音乐。%n%nXM5 Studio 只为当前 Windows 用户安装。打开软件后再选择音乐文件夹。
english.StartMenu=Create a Start menu shortcut
chinesesimplified.StartMenu=创建开始菜单快捷方式
english.Desktop=Create a desktop shortcut
chinesesimplified.Desktop=创建桌面快捷方式
english.NotOwned=Choose an empty folder or an existing XM5 Studio installation. Portable folders and unrelated files cannot be converted by setup.
chinesesimplified.NotOwned=请选择空文件夹或已有的 XM5 Studio 安装目录。安装程序不能直接覆盖便携版或其他文件夹。
english.LocalOnly=Choose a writable folder on a local fixed drive. The installation folder must be separate from your XM5 Studio profile.
chinesesimplified.LocalOnly=请选择本地固定磁盘上可写的文件夹。安装目录必须与 XM5 Studio 用户数据目录分开。
english.CloseStudio=Close XM5 Studio and its scripts before continuing. Finish or cancel active exports in the application first. Setup will not close or terminate them.
chinesesimplified.CloseStudio=请先关闭 XM5 Studio 及其脚本，并在软件中完成或取消正在进行的导出。安装程序不会关闭或强制终止它们。
english.Modified=An obsolete installed file has changed or cannot be verified. Restore it from the previous package, or choose a new empty folder. File: %1
chinesesimplified.Modified=旧版程序文件已被修改或无法验证。请从旧安装包恢复该文件，或选择新的空文件夹。文件：%1
english.Guide=Read the installation guide
chinesesimplified.Guide=阅读安装说明
english.Failure=Installation could not complete. Your profile is retained. Close XM5 Studio and rerun setup. Details are in the setup log:
chinesesimplified.Failure=安装未能完成。用户数据已保留。请关闭 XM5 Studio 后重新安装。详细信息请查看安装日志：

[Messages]
english.WelcomeLabel2={cm:Welcome}
chinesesimplified.WelcomeLabel2={cm:Welcome}
english.FinishedLabel=XM5 Studio is ready. Your library profile is stored separately in your Windows account and is retained when you uninstall.
chinesesimplified.FinishedLabel=XM5 Studio 已安装。曲库数据保存在当前 Windows 用户的独立目录中，卸载时会保留。

[Tasks]
Name: "startmenu"; Description: "{cm:StartMenu}"
Name: "desktop"; Description: "{cm:Desktop}"

[Files]
#include PayloadFiles
Source: "{#PayloadDir}\OWNED-FILES.sha256"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\XM5 Studio\XM5 Studio"; Filename: "{app}\XM5Studio.exe"; WorkingDir: "{app}"; Tasks: startmenu; AppUserModelID: "XM5Studio.Desktop"
Name: "{autodesktop}\XM5 Studio"; Filename: "{app}\XM5Studio.exe"; WorkingDir: "{app}"; Tasks: desktop; AppUserModelID: "XM5Studio.Desktop"

[Run]
Filename: "{app}\XM5Studio.exe"; Description: "{cm:LaunchProgram,XM5 Studio}"; Flags: nowait postinstall skipifsilent runasoriginaluser
Filename: "{app}\docs\INSTALLATION.md"; Description: "{cm:Guide}"; Flags: shellexec postinstall skipifsilent unchecked

[Code]
const
  Identity = 'XM5Studio:167851CF-0701-4B99-8FE4-AC2EB066C7D8';
  ActivityMutex = 'Global\XM5Studio.Application.167851CF-0701-4B99-8FE4-AC2EB066C7D8';

function GetFileAttributes(Name: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function GetDriveType(Root: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function NoLinks(Path: String): Boolean;
var Attributes: Cardinal; Parent: String;
begin
  Result := False;
  while Path <> '' do begin
    Attributes := GetFileAttributes(PathConvertNormalToSuper(Path));
    if (Attributes <> $FFFFFFFF) and ((Attributes and $400) <> 0) then exit;
    Parent := ExtractFileDir(Path);
    if CompareText(Parent, Path) = 0 then break;
    Path := Parent;
  end;
  Result := True;
end;

function OwnedDirectory(Directory: String): Boolean;
var Lines: TArrayOfString;
begin
  Result := LoadStringsFromFile(AddBackslash(Directory) + '.xm5-studio-installation', Lines);
  if Result then Result := (GetArrayLength(Lines) = 1) and (Lines[0] = Identity);
end;

function EmptyDirectory(Directory: String): Boolean;
var Record_: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Directory) + '*', Record_) then begin
    try
      repeat
        if (Record_.Name <> '.') and (Record_.Name <> '..') then begin
          Result := False; break;
        end;
      until not FindNext(Record_);
    finally
      FindClose(Record_);
    end;
  end;
end;

function ValidateDirectory(Directory: String): String;
var Profile, Parent, Probe: String;
begin
  Result := '';
  Directory := RemoveBackslashUnlessRoot(ExpandFileName(Directory));
  Profile := ExpandConstant('{localappdata}\XM5Studio');
  if (GetDriveType(AddBackslash(ExtractFileDrive(Directory))) <> 3) or not NoLinks(Directory) or
     (CompareText(Directory, Profile) = 0) or
     (Pos(Lowercase(AddBackslash(Profile)), Lowercase(AddBackslash(Directory))) = 1) or
     (Pos(Lowercase(AddBackslash(Directory)), Lowercase(AddBackslash(Profile))) = 1) then begin
    Result := CustomMessage('LocalOnly'); exit;
  end;
  if DirExists(Directory) and not EmptyDirectory(Directory) and not OwnedDirectory(Directory) then begin
    Result := CustomMessage('NotOwned'); exit;
  end;
  Parent := Directory;
  while not DirExists(Parent) do Parent := ExtractFileDir(Parent);
  Probe := GenerateUniqueName(Parent, '.tmp');
  if not SaveStringToFile(Probe, '', False) then Result := CustomMessage('LocalOnly')
  else DeleteFile(Probe);
end;

function RelativeEntry(Line: String; var Path, Hash: String): Boolean;
var Separator, Index: Integer;
begin
  Result := False;
  Separator := Pos(#9, Line);
  if Separator <> 65 then exit;
  Hash := Copy(Line, 1, 64);
  for Index := 1 to 64 do if Pos(Lowercase(Hash[Index]), '0123456789abcdef') = 0 then exit;
  Path := Copy(Line, 66, Length(Line));
  if (Path = '') or (Path[1] = '/') or (Pos('..', Path) > 0) or (Pos('\', Path) > 0) or
     (Pos(':', Path) > 0) or (Pos('*', Path) > 0) or (Pos('?', Path) > 0) or
     (Pos(#9, Path) > 0) or (Pos('//', Path) > 0) then exit;
  Result := True;
end;

function InspectObsolete(Delete_: Boolean): String;
var Previous, Current: TArrayOfString; Index: Integer; Path, Hash, Target: String;
    CurrentPaths: TStringList;
begin
  Result := '';
  if not OwnedDirectory(ExpandConstant('{app}')) then exit;
  if not LoadStringsFromFile(ExpandConstant('{app}\OWNED-FILES.sha256'), Previous) or
     not LoadStringsFromFile(ExpandConstant('{tmp}\OWNED-FILES.sha256'), Current) then begin
    Result := CustomMessage('NotOwned'); exit;
  end;
  CurrentPaths := TStringList.Create;
  try
  CurrentPaths.Sorted := True;
  for Index := 0 to GetArrayLength(Current) - 1 do begin
    if not RelativeEntry(Current[Index], Path, Hash) then begin Result := CustomMessage('NotOwned'); exit; end;
    CurrentPaths.Add(Lowercase(Path));
  end;
  for Index := 0 to GetArrayLength(Previous) - 1 do begin
    if not RelativeEntry(Previous[Index], Path, Hash) then begin Result := CustomMessage('NotOwned'); exit; end;
    if CurrentPaths.IndexOf(Lowercase(Path)) < 0 then begin
      Target := ExpandConstant('{app}\') + Path;
      StringChangeEx(Target, '/', '\', True);
      if not NoLinks(Target) then begin Result := FmtMessage(CustomMessage('Modified'), [Path]); exit; end;
      if FileExists(Target) then begin
        if CompareText(GetSHA256OfFile(Target), Hash) <> 0 then begin
          Result := FmtMessage(CustomMessage('Modified'), [Path]); exit;
        end;
        if Delete_ and not DeleteFile(Target) then begin Result := FmtMessage(CustomMessage('Modified'), [Path]); exit; end;
      end;
    end;
  end;
  finally
    CurrentPaths.Free;
  end;
end;

procedure InitializeWizard;
begin
  Log('XM5 Studio installs for the current user; profile data is preserved.');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var Error_: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Error_ := ValidateDirectory(WizardDirValue);
    if Error_ <> '' then begin MsgBox(Error_, mbError, MB_OK); Result := False; end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := ValidateDirectory(ExpandConstant('{app}'));
  if Result <> '' then exit;
  if CheckForMutexes(ActivityMutex) then begin Result := CustomMessage('CloseStudio'); exit; end;
  ExtractTemporaryFile('OWNED-FILES.sha256');
  Result := InspectObsolete(False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Error_, Language_, Descriptor: String;
begin
  if CurStep = ssInstall then begin
    if CheckForMutexes(ActivityMutex) then RaiseException(CustomMessage('CloseStudio'));
    Error_ := InspectObsolete(True);
    if Error_ <> '' then RaiseException(Error_);
  end;
  if CurStep = ssPostInstall then begin
    Language_ := 'en';
    if ActiveLanguage = 'chinesesimplified' then Language_ := 'zh';
    Descriptor := '{"schema":"xm5.distribution.v1","mode":"installed","initial_language":"' + Language_ + '"}' + #10;
    if not SaveStringToFile(ExpandConstant('{app}\distribution.json'), Descriptor, False) then
      RaiseException(CustomMessage('LocalOnly'));
  end;
end;

function InitializeUninstall: Boolean;
begin
  CreateMutex('Global\XM5Studio.Setup.167851CF-0701-4B99-8FE4-AC2EB066C7D8');
  Result := not CheckForMutexes(ActivityMutex);
  if not Result then SuppressibleMsgBox(CustomMessage('CloseStudio'), mbError, MB_OK, IDOK);
end;

procedure DeinitializeSetup;
begin
  Log('Setup log: ' + ExpandConstant('{log}'));
end;
