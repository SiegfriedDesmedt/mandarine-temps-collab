Option Explicit

Dim fileSystem, shell, projectDirectory, launcherPath, command

Set fileSystem = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projectDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
launcherPath = fileSystem.BuildPath(projectDirectory, "tools\lancer_application.ps1")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Quote(launcherPath)

shell.Run command, 0, False

Function Quote(ByVal value)
    Quote = Chr(34) & value & Chr(34)
End Function
