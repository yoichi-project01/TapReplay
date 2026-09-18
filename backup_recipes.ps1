[CmdletBinding()]
param(
    # AllowEmptyString + a manual check further down (rather than relying
    # on Mandatory's own default validation) is deliberate: a plain
    # [string] Mandatory parameter already rejects an empty string, but
    # with PowerShell's own generic
    # "ParameterArgumentValidationErrorEmptyStringNotAllowed" message,
    # which does not say this script is the one calling out the problem,
    # nor point at build.bat as the likely source. Confirmed this exact
    # message is what a caller passing "" produces.
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$SourceDir,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$BackupDir
)

if ([string]::IsNullOrWhiteSpace($SourceDir) -or [string]::IsNullOrWhiteSpace($BackupDir)) {
    Write-Output "ERROR: backup_recipes.ps1 was called with an empty -SourceDir or -BackupDir."
    Write-Output "  SourceDir=[$SourceDir]"
    Write-Output "  BackupDir=[$BackupDir]"
    Write-Output "This is a bug in build.bat's own call to this script, not anything"
    Write-Output "about your recipes or a locked file. If you just edited build.bat,"
    Write-Output "look for a variable read with %VAR% inside the same '( ... )' block"
    Write-Output "where it was set - without setlocal enabledelayedexpansion, that"
    Write-Output "reads the value from before the block started, not what an earlier"
    Write-Output "line in the same block just set. Re-run 'build.bat debug' to see"
    Write-Output "what build.bat actually computed for these paths."
    exit 1
}

# Called from build.bat before the PyInstaller build, to safely relocate
# dist\TapReplay\recipes\ (user-recorded data) out of the way. PyInstaller
# deletes the entire dist\TapReplay\ tree before rebuilding it, so nothing
# under $SourceDir can be left behind afterward - even a single locked
# file there makes PyInstaller's own cleanup crash with the exact same
# kind of error this script is meant to explain clearly instead.
#
# Copies each file individually (rather than moving/renaming the whole
# folder in one shot, as this used to do) for two reasons:
#   1. A single locked file used to fail the entire move with one opaque
#      OS message ("Access is denied") and no filename. This reports
#      exactly which file, and - via the Restart Manager API, the same
#      mechanism Windows Explorer itself uses for "this file is open in
#      <program>" - which process holds it, when that can be determined.
#   2. Copying only needs read access, which most locks still allow. For
#      example gui.py's own playback log is opened with open(path, "a"),
#      which denies delete but still allows shared read/write, so the
#      copy itself usually succeeds even for a currently-open log. It is
#      the follow-up delete of the original that then fails, and that
#      too is reported with the same file+process detail.
#
# Every file still has to end up safely copied AND removed from
# $SourceDir before this script reports success (exit 0), regardless of
# whether it looks disposable (a *.log) or is the recipe itself
# (recipe.json, template/mask images). This is not a relaxed safety bar:
# PyInstaller's own full-directory wipe cannot tolerate a single leftover
# file of any kind, so a failure that only touches *.log files still
# stops this script (exit 1) - it just gets a calmer message than a
# failure touching recipe.json or a template/mask image, since only the
# latter is an actual data-loss risk.

$ErrorActionPreference = "Stop"
$RETRY_COUNT = 3
$RETRY_DELAY_MS = 400

# --- Restart Manager P/Invoke: "who has this file open" --------------
# WhoIsLocking itself is a handful of fast, local Win32 calls and should
# return in well under a second - but it is still an OS API this script
# does not control, so WhoIsLockingWithTimeout runs it on a background
# thread and gives up (reporting "couldn't tell" rather than hanging)
# if it does not return promptly. Diagnosing a lock must never itself
# risk turning a quick, clear error into a stuck build.
$rmSig = @"
using System;
using System.Runtime.InteropServices;
using System.Threading.Tasks;

public class TapReplayRm {
    [StructLayout(LayoutKind.Sequential)]
    public struct RM_UNIQUE_PROCESS {
        public int dwProcessId;
        public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
    }

    const int CCH_RM_MAX_APP_NAME = 255;
    const int CCH_RM_MAX_SVC_NAME = 63;

    public enum RM_APP_TYPE {
        RmUnknownApp = 0, RmMainWindow = 1, RmOtherWindow = 2,
        RmService = 3, RmExplorer = 4, RmConsole = 5, RmCritical = 1000
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct RM_PROCESS_INFO {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_APP_NAME + 1)]
        public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_SVC_NAME + 1)]
        public string strServiceShortName;
        public RM_APP_TYPE ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)]
        public bool bRestartable;
    }

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, string strSessionKey);
    [DllImport("rstrtmgr.dll")]
    static extern int RmEndSession(uint pSessionHandle);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, string[] rgsFilenames,
        uint nApplications, RM_UNIQUE_PROCESS[] rgApplications, uint nServices, string[] rgsServiceNames);
    [DllImport("rstrtmgr.dll")]
    static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo,
        [In, Out] RM_PROCESS_INFO[] rgAffectedApps, ref uint lpdwRebootReasons);

    // Best-effort: returns a short description of the process(es) holding
    // "path" open, or an empty array if none could be determined. Never
    // throws - callers treat "couldn't tell" the same as "no lock found".
    public static string[] WhoIsLocking(string path) {
        uint handle;
        string key = new string(new char[16 + 1]);
        if (RmStartSession(out handle, 0, key) != 0) return new string[0];
        try {
            string[] resources = new string[] { path };
            if (RmRegisterResources(handle, 1, resources, 0, null, 0, null) != 0) return new string[0];

            uint needed = 0, count = 0, rebootReasons = 0;
            int res = RmGetList(handle, out needed, ref count, null, ref rebootReasons);
            if ((res != 0 && res != 234 /* ERROR_MORE_DATA */) || needed == 0) return new string[0];

            RM_PROCESS_INFO[] info = new RM_PROCESS_INFO[needed];
            count = needed;
            if (RmGetList(handle, out needed, ref count, info, ref rebootReasons) != 0) return new string[0];

            string[] outArr = new string[count];
            for (int i = 0; i < count; i++) {
                outArr[i] = info[i].strAppName + " (PID " + info[i].Process.dwProcessId + ")";
            }
            return outArr;
        } catch {
            return new string[0];
        } finally {
            RmEndSession(handle);
        }
    }

    // Same as WhoIsLocking, but gives up after timeoutMs instead of
    // potentially blocking forever on an OS call this script does not
    // control. The abandoned background task (if any) is left to finish
    // on its own; it does not block this method's return.
    public static string[] WhoIsLockingWithTimeout(string path, int timeoutMs) {
        try {
            Task<string[]> task = Task.Run(() => WhoIsLocking(path));
            if (task.Wait(timeoutMs)) {
                return task.Result;
            }
        } catch {
        }
        return new string[0];
    }
}
"@

try {
    Add-Type -TypeDefinition $rmSig -Language CSharp -ErrorAction Stop
    $rmAvailable = $true
} catch {
    $rmAvailable = $false
}

function Get-Locker([string]$path) {
    if (-not $rmAvailable) { return "unknown process" }
    try {
        $who = [TapReplayRm]::WhoIsLockingWithTimeout($path, 3000)
        if ($who.Length -eq 0) { return "unknown process" }
        return ($who -join ", ")
    } catch {
        return "unknown process"
    }
}

function Try-Op([scriptblock]$op) {
    for ($i = 0; $i -lt $RETRY_COUNT; $i++) {
        try {
            & $op
            return $true
        } catch {
            if ($i -lt $RETRY_COUNT - 1) { Start-Sleep -Milliseconds $RETRY_DELAY_MS }
        }
    }
    return $false
}

if (-not (Test-Path -LiteralPath $SourceDir)) {
    exit 0
}

if (-not (Test-Path -LiteralPath $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$sourceFull = (Resolve-Path -LiteralPath $SourceDir).Path.TrimEnd("\")
$criticalFailures = @()
$logFailures = @()

$files = Get-ChildItem -LiteralPath $sourceFull -Recurse -File
foreach ($f in $files) {
    $rel = $f.FullName.Substring($sourceFull.Length).TrimStart("\")
    $dest = Join-Path $BackupDir $rel
    $destDir = Split-Path -Parent $dest
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }

    # .log is the only extension treated as disposable (matches gui.py's
    # playback_*.log). Everything else - recipe.json, failures.jsonl,
    # every template/mask/context image - is treated as real recipe data.
    $isCritical = ($f.Extension -ne ".log")

    $copyOk = Try-Op { Copy-Item -LiteralPath $f.FullName -Destination $dest -Force -ErrorAction Stop }
    if (-not $copyOk) {
        $who = Get-Locker $f.FullName
        $msg = "$rel - could not back it up (held open by $who)"
        if ($isCritical) { $criticalFailures += $msg } else { $logFailures += $msg }
        continue
    }

    $deleteOk = Try-Op { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop }
    if (-not $deleteOk) {
        $who = Get-Locker $f.FullName
        $msg = "$rel - backed up, but the original is still locked (held open by $who) and blocks the build"
        if ($isCritical) { $criticalFailures += $msg } else { $logFailures += $msg }
    }
}

# Clean up now-empty directories left behind (deepest first), and finally
# the recipes root itself if it emptied out completely. A directory that
# still contains an undeletable file is simply left as-is - the failure
# list above already explains why.
Get-ChildItem -LiteralPath $sourceFull -Recurse -Directory |
    Sort-Object { ($_.FullName -split "\\").Count } -Descending |
    ForEach-Object {
        try { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop } catch {}
    }
try { Remove-Item -LiteralPath $sourceFull -Force -ErrorAction Stop } catch {}

if ($criticalFailures.Count -gt 0) {
    Write-Output "CRITICAL: the following recipe file(s) could not be safely backed up:"
    foreach ($m in $criticalFailures) { Write-Output "  $m" }
    if ($logFailures.Count -gt 0) {
        Write-Output "Also still locked (less urgent - log/history files only):"
        foreach ($m in $logFailures) { Write-Output "  $m" }
    }
    Write-Output "Nothing else was touched for these files. Close whatever is holding"
    Write-Output "them open, then re-run build.bat."
    exit 1
}

if ($logFailures.Count -gt 0) {
    Write-Output "Your recipes were backed up successfully to:"
    Write-Output "  $BackupDir"
    Write-Output "However, the following log/history file(s) are still locked, and"
    Write-Output "PyInstaller needs this folder completely clear to rebuild it:"
    foreach ($m in $logFailures) { Write-Output "  $m" }
    Write-Output "Close whatever is holding them open, then re-run build.bat."
    exit 1
}

exit 0
