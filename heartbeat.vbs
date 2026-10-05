Rem Common Room heartbeat, hidden: runs heartbeat.cmd <project dir> with no console window.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & here & "\heartbeat.cmd"" """ & WScript.Arguments(0) & """", 0, False
