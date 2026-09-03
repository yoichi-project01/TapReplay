[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetPath,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string]$IconPath
)

# Called from build.bat after a successful build to place a desktop
# shortcut. Prints "OK:<path>" on success or "FAIL:<message>" on failure
# and exits 0/1 accordingly - build.bat parses that prefix and treats any
# failure here as a non-fatal warning, since the real build output is
# already safely in dist\TapReplay regardless of what happens below.
try {
    # [Environment]::GetFolderPath resolves the real, possibly
    # OneDrive-redirected Desktop path instead of assuming
    # %USERPROFILE%\Desktop.
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktop 'TapReplay.lnk'

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = $WorkingDirectory
    if (Test-Path -LiteralPath $IconPath) {
        $shortcut.IconLocation = $IconPath
    }
    $shortcut.Save()

    Write-Output "OK:$shortcutPath"
    exit 0
} catch {
    Write-Output "FAIL:$($_.Exception.Message)"
    exit 1
}
