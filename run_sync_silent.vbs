' ==============================================================================
' MyWhoosh2Garmin Silent Background Runner
' Designed for Windows Task Scheduler
' ==============================================================================
Option Explicit

Dim objShell, objFSO, scriptDir, scriptPath, cmd

Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

' Get the absolute directory where this VBS script resides
scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
scriptPath = scriptDir & "\myWhoosh2Garmin.py"

' Verify that the Python script exists
If Not objFSO.FileExists(scriptPath) Then
    WScript.Quit 1
End If

' Execute via pythonw or pyw (window style 0 = hidden, wait = false)
' Using cmd /c with hidden window ensures environment PATH is loaded
cmd = "cmd.exe /c cd /d """ & scriptDir & """ && (where pythonw >nul 2>nul && pythonw """ & scriptPath & """ || py -3 """ & scriptPath & """)"

objShell.Run cmd, 0, False

Set objShell = Nothing
Set objFSO = Nothing
