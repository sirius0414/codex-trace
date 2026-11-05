# Forward all arguments as an array; never invoke a command string.
$ErrorActionPreference = 'Stop'
$Installer = Join-Path $PSScriptRoot 'install.py'
if ($env:CODEX_TRACE_PYTHON) {
    & $env:CODEX_TRACE_PYTHON -B $Installer @args
    exit $global:LASTEXITCODE
}
foreach ($Candidate in @('python3.14', 'python3', 'python', 'py')) {
    if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
        $Prefix = @()
        if ($Candidate -eq 'py') { $Prefix = @('-3.14') }
        # Windows PowerShell 5.1 represents redirected native stderr as an
        # ErrorRecord. A missing compression module is an expected probe miss,
        # so it must not terminate the launcher under ErrorActionPreference=Stop.
        $Supported = $false
        $SavedErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            # Native processes update the global exit code; a script-local value shadows it.
            $global:LASTEXITCODE = 1
            & $Candidate @Prefix -I -c 'import sys; from compression import zstd; assert sys.version_info >= (3,14)' 2>$null
            $Supported = ($global:LASTEXITCODE -eq 0)
        } catch {
            $Supported = $false
        } finally {
            $ErrorActionPreference = $SavedErrorPreference
        }
        if ($Supported) {
            & $Candidate @Prefix -B $Installer @args
            exit $global:LASTEXITCODE
        }
    }
}
Write-Error 'Python 3.14+ is required. Install from https://www.python.org/downloads/ or set CODEX_TRACE_PYTHON to the interpreter path.'
exit 1
