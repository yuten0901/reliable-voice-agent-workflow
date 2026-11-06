[CmdletBinding()]
param(
    [switch]$KeepTemporary
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [string[]]$ArgumentList,
        [Parameter(Mandatory)] [string]$WorkingDirectory
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed ($LASTEXITCODE): $FilePath $($ArgumentList -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-RepositoryFingerprint {
    param([Parameter(Mandatory)] [string]$Repository)

    $head = & git -C $Repository rev-parse --verify HEAD 2>$null
    if ($LASTEXITCODE -ne 0) {
        $head = "<unborn>"
    }
    $index = (& git -C $Repository ls-files --stage | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the original Git index."
    }
    $status = (& git -C $Repository status --porcelain=v1 --untracked-files=all | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the original Git worktree."
    }
    $value = "$head`n--INDEX--`n$index`n--STATUS--`n$status"
    $bytes = [Text.Encoding]::UTF8.GetBytes($value)
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
}

function Get-CanonicalJson {
    param([Parameter(Mandatory)] [string]$Path)

    return (Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress)
}

$repositoryRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$manifestPath = Join-Path $repositoryRoot "release-manifest.txt"
$toolPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$workspace = [IO.Path]::GetFullPath(
    (Join-Path $tempBase ("voice-agent-release-verification-" + [guid]::NewGuid().ToString("N")))
)
$workspaceLeaf = Split-Path -Leaf $workspace
if (-not $workspace.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
    -not $workspaceLeaf.StartsWith("voice-agent-release-verification-", [StringComparison]::Ordinal)) {
    throw "Unsafe temporary workspace path: $workspace"
}
if (-not (Test-Path -LiteralPath $toolPython -PathType Leaf)) {
    throw "Local verification environment is missing: $toolPython"
}

$beforeFingerprint = Get-RepositoryFingerprint -Repository $repositoryRoot
$failure = $null
$commitId = $null

try {
    $entries = @(
        Get-Content -LiteralPath $manifestPath |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -ne "" }
    )
    if ($entries.Count -eq 0) {
        throw "Release manifest is empty."
    }
    if (($entries | Select-Object -Unique).Count -ne $entries.Count) {
        throw "Release manifest contains duplicate paths."
    }
    if ($entries -notcontains "release-manifest.txt" -or
        $entries -notcontains "scripts/verify-release.ps1") {
        throw "The manifest must include itself and the release verifier."
    }

    $seed = Join-Path $workspace "seed"
    $clone = Join-Path $workspace "clone"
    $artifacts = Join-Path $workspace "artifacts"
    New-Item -ItemType Directory -Path $seed, $artifacts | Out-Null

    foreach ($entry in $entries) {
        if ($entry.Contains("\") -or [IO.Path]::IsPathRooted($entry) -or
            $entry -eq ".." -or $entry.StartsWith("../", [StringComparison]::Ordinal) -or
            $entry.Contains("/../")) {
            throw "Manifest path is not a safe repository-relative POSIX path: $entry"
        }
        $source = [IO.Path]::GetFullPath((Join-Path $repositoryRoot $entry))
        $rootPrefix = $repositoryRoot.TrimEnd("\") + "\"
        if (-not $source.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Manifest entry is missing or escapes the repository: $entry"
        }
        $destination = Join-Path $seed $entry
        $destinationParent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationParent)) {
            New-Item -ItemType Directory -Path $destinationParent | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $destination
    }

    Invoke-Checked -FilePath "git" -ArgumentList @("init", "--initial-branch=main") -WorkingDirectory $seed
    Invoke-Checked -FilePath "git" -ArgumentList @("config", "user.name", "Local Release Verifier") -WorkingDirectory $seed
    Invoke-Checked -FilePath "git" -ArgumentList @("config", "user.email", "release-verifier@invalid.example") -WorkingDirectory $seed
    Invoke-Checked -FilePath "git" -ArgumentList @("config", "commit.gpgsign", "false") -WorkingDirectory $seed
    Invoke-Checked -FilePath "git" -ArgumentList @("config", "core.autocrlf", "false") -WorkingDirectory $seed
    Invoke-Checked -FilePath "git" -ArgumentList @("add", "--all") -WorkingDirectory $seed
    Invoke-Checked -FilePath "git" -ArgumentList @("commit", "-m", "test(release): verify public voice agent package") -WorkingDirectory $seed
    $commitId = (& git -C $seed rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve temporary verification commit."
    }
    Invoke-Checked -FilePath "git" -ArgumentList @("clone", "--local", "--no-hardlinks", $seed, $clone) -WorkingDirectory $workspace
    Invoke-Checked -FilePath "git" -ArgumentList @(
        "-c", "core.whitespace=blank-at-eol,space-before-tab,-blank-at-eof", "diff-tree", "--check", "--root", "HEAD"
    ) -WorkingDirectory $clone

    $cloneFiles = @(
        Get-ChildItem -LiteralPath $clone -Recurse -Force -File |
            ForEach-Object {
                [IO.Path]::GetRelativePath($clone, $_.FullName).Replace("\", "/")
            } |
            Where-Object { -not $_.StartsWith(".git/", [StringComparison]::Ordinal) } |
            Sort-Object
    )
    $expectedFiles = @($entries | Sort-Object)
    $fileDifference = @(Compare-Object -ReferenceObject $expectedFiles -DifferenceObject $cloneFiles)
    if ($fileDifference.Count -ne 0) {
        throw "Local clone does not exactly match release-manifest.txt: $($fileDifference | Out-String)"
    }

    $secretPatterns = @(
        "AKIA[0-9A-Z]{16}",
        "sk-(live|test)-[A-Za-z0-9]{16,}",
        "gh[pousr]_[A-Za-z0-9_]{20,}",
        "-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "xox[baprs]-[A-Za-z0-9-]+"
    )
    foreach ($entry in $entries) {
        $candidatePath = Join-Path $clone $entry
        if ([IO.Path]::GetExtension($candidatePath) -ieq ".png") {
            $content = [Text.Encoding]::Latin1.GetString([IO.File]::ReadAllBytes($candidatePath))
        }
        else {
            $content = Get-Content -Raw -LiteralPath $candidatePath
        }
        foreach ($pattern in $secretPatterns) {
            if ($content -match $pattern) {
                throw "Representative secret pattern found in public candidate: $entry"
            }
        }
    }

    $coverPath = Join-Path $clone "docs\portfolio-cover.png"
    $coverBytes = [IO.File]::ReadAllBytes($coverPath)
    $expectedPngSignature = [byte[]](137, 80, 78, 71, 13, 10, 26, 10)
    if ($coverBytes.Length -lt 24 -or
        ($coverBytes[0..7] -join ",") -cne ($expectedPngSignature -join ",")) {
        throw "Portfolio cover is not a valid PNG signature."
    }
    [uint32]$coverWidth = ([uint32]$coverBytes[16] -shl 24) -bor
        ([uint32]$coverBytes[17] -shl 16) -bor
        ([uint32]$coverBytes[18] -shl 8) -bor [uint32]$coverBytes[19]
    [uint32]$coverHeight = ([uint32]$coverBytes[20] -shl 24) -bor
        ([uint32]$coverBytes[21] -shl 16) -bor
        ([uint32]$coverBytes[22] -shl 8) -bor [uint32]$coverBytes[23]
    if ($coverWidth -ne 1000 -or $coverHeight -ne 750) {
        throw "Unexpected portfolio cover dimensions: ${coverWidth}x${coverHeight}"
    }

    $privateValues = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($fixturePath in Get-ChildItem -LiteralPath (Join-Path $clone "fixtures\calls") -Filter "*.json") {
        $fixture = Get-Content -Raw -LiteralPath $fixturePath.FullName | ConvertFrom-Json
        [void]$privateValues.Add([string]$fixture.call_id)
        [void]$privateValues.Add([string]$fixture.caller_id)
        foreach ($turn in $fixture.turns) {
            [void]$privateValues.Add([string]$turn.audio_ref)
            if (-not [string]::IsNullOrWhiteSpace([string]$turn.transcript)) {
                [void]$privateValues.Add([string]$turn.transcript)
                foreach ($email in [regex]::Matches([string]$turn.transcript, "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")) {
                    [void]$privateValues.Add($email.Value)
                }
                $nameMatch = [regex]::Match([string]$turn.transcript, "(?i)my name is ([A-Za-z]+(?: [A-Za-z]+)+)")
                if ($nameMatch.Success) {
                    [void]$privateValues.Add($nameMatch.Groups[1].Value.TrimEnd("."))
                }
            }
        }
    }
    $safeSurfaceEntries = @(
        "README.md",
        "docs/architecture.md",
        "docs/booking-flow.svg",
        "docs/deployment-boundary.md",
        "docs/evaluation.md",
        "reports/evaluation.json"
    )
    foreach ($entry in $safeSurfaceEntries) {
        $content = Get-Content -Raw -LiteralPath (Join-Path $clone $entry)
        foreach ($privateValue in $privateValues) {
            if (-not [string]::IsNullOrWhiteSpace($privateValue) -and $content.Contains($privateValue, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Fixture PII/value leaked into a public safe surface: $entry"
            }
        }
    }

    $readme = Get-Content -Raw -LiteralPath (Join-Path $clone "README.md")
    foreach ($requiredBoundary in @(
        "without making a real call",
        "production telephony experience",
        "No Twilio, Vapi or Retell integration"
    )) {
        if (-not $readme.Contains($requiredBoundary, [StringComparison]::Ordinal)) {
            throw "README evidence boundary is missing: $requiredBoundary"
        }
    }
    $license = Get-Content -Raw -LiteralPath (Join-Path $clone "LICENSE")
    $pyproject = Get-Content -Raw -LiteralPath (Join-Path $clone "pyproject.toml")
    if (-not $license.StartsWith("MIT License", [StringComparison]::Ordinal) -or
        -not $pyproject.Contains('license = { text = "MIT" }', [StringComparison]::Ordinal)) {
        throw "MIT license metadata is incomplete."
    }

    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = Join-Path $clone "src"
        Invoke-Checked -FilePath $toolPython -ArgumentList @("-m", "pytest", "-q") -WorkingDirectory $clone
        Invoke-Checked -FilePath $toolPython -ArgumentList @("-m", "ruff", "check", ".") -WorkingDirectory $clone
        Invoke-Checked -FilePath $toolPython -ArgumentList @("-m", "mypy", "src", "tests") -WorkingDirectory $clone
        Invoke-Checked -FilePath $toolPython -ArgumentList @("-m", "compileall", "-q", "src", "tests") -WorkingDirectory $clone
        $generatedEvaluation = Join-Path $artifacts "source-evaluation.json"
        Invoke-Checked -FilePath $toolPython -ArgumentList @(
            "-m", "voice_agent_demo.cli", "evaluate", "fixtures/calls", "--output", $generatedEvaluation
        ) -WorkingDirectory $clone
    }
    finally {
        $env:PYTHONPATH = $oldPythonPath
    }
    if ((Get-CanonicalJson -Path $generatedEvaluation) -cne
        (Get-CanonicalJson -Path (Join-Path $clone "reports\evaluation.json"))) {
        throw "Committed evaluation report does not match a fresh source evaluation."
    }

    $pyLauncher = (Get-Command "py" -ErrorAction Stop).Source
    $wheelDirectory = Join-Path $artifacts "wheel"
    New-Item -ItemType Directory -Path $wheelDirectory | Out-Null
    Invoke-Checked -FilePath $pyLauncher -ArgumentList @(
        "-3.11", "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", $wheelDirectory
    ) -WorkingDirectory $clone
    $wheel = @(Get-ChildItem -LiteralPath $wheelDirectory -Filter "*.whl")
    if ($wheel.Count -ne 1) {
        throw "Expected exactly one wheel, found $($wheel.Count)."
    }
    $wheelArchive = [IO.Compression.ZipFile]::OpenRead($wheel[0].FullName)
    try {
        if (-not ($wheelArchive.Entries | Where-Object { $_.FullName -match "\.dist-info/LICENSE$" })) {
            throw "Built wheel does not contain the MIT license file."
        }
        foreach ($entry in $wheelArchive.Entries | Where-Object { $_.Length -gt 0 }) {
            $reader = [IO.StreamReader]::new($entry.Open(), [Text.Encoding]::UTF8, $true)
            try {
                $entryContent = $reader.ReadToEnd()
            }
            finally {
                $reader.Dispose()
            }
            foreach ($pattern in $secretPatterns) {
                if ($entryContent -match $pattern) {
                    throw "Representative secret pattern found in wheel entry: $($entry.FullName)"
                }
            }
            foreach ($privateValue in $privateValues) {
                if (-not [string]::IsNullOrWhiteSpace($privateValue) -and
                    $entryContent.Contains($privateValue, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Fixture PII/value found in wheel entry: $($entry.FullName)"
                }
            }
        }
    }
    finally {
        $wheelArchive.Dispose()
    }

    $freshVenv = Join-Path $workspace "fresh-venv"
    Invoke-Checked -FilePath $pyLauncher -ArgumentList @("-3.11", "-m", "venv", $freshVenv) -WorkingDirectory $workspace
    $freshPython = Join-Path $freshVenv "Scripts\python.exe"
    $freshCli = Join-Path $freshVenv "Scripts\voice-agent-demo.exe"
    Invoke-Checked -FilePath $freshPython -ArgumentList @("-m", "pip", "install", "--no-index", $wheel[0].FullName) -WorkingDirectory $workspace
    Invoke-Checked -FilePath $freshCli -ArgumentList @("--help") -WorkingDirectory $workspace
    $wheelEvaluation = Join-Path $artifacts "wheel-evaluation.json"
    Invoke-Checked -FilePath $freshCli -ArgumentList @(
        "evaluate", (Join-Path $clone "fixtures\calls"), "--output", $wheelEvaluation
    ) -WorkingDirectory $workspace
    if ((Get-CanonicalJson -Path $wheelEvaluation) -cne
        (Get-CanonicalJson -Path (Join-Path $clone "reports\evaluation.json"))) {
        throw "Fresh-wheel evaluation does not match the committed report."
    }

    $cloneStatus = @(& git -C $clone status --porcelain=v1 --untracked-files=all)
    $allowedGenerated = @(
        ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", "src/voice_agent_demo.egg-info/"
    )
    $unexpected = @(
        $cloneStatus | Where-Object {
            $line = $_
            -not ($allowedGenerated | Where-Object { $line.Replace("\", "/").Contains($_) })
        }
    )
    if ($unexpected.Count -ne 0) {
        throw "Verification changed tracked or unexpected files in the clone: $($unexpected -join '; ')"
    }

    Write-Output "release_verification=PASS"
    Write-Output "temporary_commit=$commitId"
    Write-Output "manifest_files=$($entries.Count)"
    Write-Output "portfolio_cover=${coverWidth}x${coverHeight}"
}
catch {
    $failure = $_
}
finally {
    if ($KeepTemporary) {
        Write-Output "temporary_workspace=$workspace"
    }
    elseif (Test-Path -LiteralPath $workspace) {
        $resolvedWorkspace = [IO.Path]::GetFullPath($workspace)
        if (-not $resolvedWorkspace.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Split-Path -Leaf $resolvedWorkspace).StartsWith("voice-agent-release-verification-", [StringComparison]::Ordinal)) {
            throw "Refusing to remove an unsafe temporary path: $resolvedWorkspace"
        }
        Remove-Item -LiteralPath $resolvedWorkspace -Recurse -Force
    }
}

$afterFingerprint = Get-RepositoryFingerprint -Repository $repositoryRoot
if ($beforeFingerprint -cne $afterFingerprint) {
    throw "Original repository HEAD, index or worktree changed during release verification."
}
if ($null -ne $failure) {
    throw $failure
}
Write-Output "original_repository_unchanged=PASS"
