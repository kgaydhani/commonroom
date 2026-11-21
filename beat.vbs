Rem Common Room beat, hidden: runs beat.cmd with no console window.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & here & "\beat.cmd""", 0, False
