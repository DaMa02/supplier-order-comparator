$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # The console keeps Windows' default encoding.
}

$skillDir = $PSScriptRoot
$launcherPath = Join-Path $skillDir "app\launcher.py"

function Mostra-IlMotivo {
    # git's own stderr lines, printed under the Italian message that states
    # what happened. They're in English and mostly noise on a PC no one
    # watches — but they're the only way to know why an update once failed
    # to apply.
    param($Righe)

    foreach ($riga in @($Righe)) {
        $testo = "$riga".Trim()
        if ($testo) { Write-Host "        $testo" -ForegroundColor DarkYellow }
    }
}

function Trova-Git {
    # This PC's git, or $null. Checks the normal install location first,
    # then falls back to PATH.
    $git = "C:\Program Files\Git\cmd\git.exe"
    if (Test-Path -LiteralPath $git) { return $git }
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Salva-IlRegistroImparato {
    # Sets references\adapters.json aside before the reset reverts it. The
    # name carries the date and time so two nearby startups don't overwrite
    # each other's copy; the last ten are kept, under 200 KB in total.
    #
    # A failure here just warns and continues: this is a safety net, not a
    # precondition for starting the program.
    param([string]$Repo)

    # The `catch` below only catches something if the error is terminating,
    # and this preference is what makes it so. The function sets it itself
    # rather than inheriting it from line 1, because `Sync-DaGitHub` — its
    # caller — sets it to "Continue" so it can read git's own error output.
    $ErrorActionPreference = "Stop"

    try {
        $cartella = Join-Path $Repo "app\data\adattatori-prima-dell-aggiornamento"
        if (-not (Test-Path -LiteralPath $cartella)) {
            New-Item -ItemType Directory -Path $cartella -Force | Out-Null
        }
        $quando = Get-Date -Format "yyyy-MM-dd_HHmmss"
        $destinazione = Join-Path $cartella "adapters_$quando.json"
        Copy-Item -LiteralPath (Join-Path $Repo "references\adapters.json") -Destination $destinazione -Force

        Get-ChildItem -LiteralPath $cartella -Filter "adapters_*.json" |
            Sort-Object Name -Descending |
            Select-Object -Skip 10 |
            Remove-Item -Force -ErrorAction SilentlyContinue

        return $destinazione
    } catch {
        Write-Host "[SYNC] Non sono riuscito a mettere da parte il registro degli adattatori ($($_.Exception.Message))." -ForegroundColor Yellow
        return $null
    }
}

function Test-ComparatoreAcceso {
    # True if THIS comparator is answering on this port, not just some
    # other program that happens to hold it.
    #
    # The check matters because `Get-NetTCPConnection` alone only says
    # "someone is listening", and that someone can be an unrelated program.
    # Skipping the sync for it would mean the store PC stops receiving
    # updates for as long as that program stays running.
    #
    # `/api/health` responds `{"ok": true, ...}` with no token required:
    # it's the same route `app/launcher.py` uses to recognize a running
    # server.
    #
    # When in doubt this answers "not running", so the sync proceeds: a
    # wrong guess here just falls back to the behavior from before this
    # check existed, which is the cheaper mistake.
    #
    # Checked in two steps, and the first exists to avoid paying for the
    # second: on a normal morning nothing is on port 8765, and finding that
    # out with an HTTP request means loading the .NET stack on every
    # startup just to be told no. `Get-NetTCPConnection` answers instantly
    # without loading anything; the "who are you" question is only asked
    # once something is actually there.
    param([int]$Porta)

    try {
        $inAscolto = Get-NetTCPConnection -LocalPort $Porta -State Listen -ErrorAction SilentlyContinue
    } catch {
        # The cmdlet isn't available (too old a Windows, missing module):
        # not worth reporting, and the cautious answer is "not running",
        # i.e. sync anyway.
        return $false
    }
    if (-not $inAscolto) { return $false }

    try {
        $risposta = Invoke-RestMethod -Uri "http://127.0.0.1:$Porta/api/health" -TimeoutSec 2 -ErrorAction Stop
        return ($risposta.ok -eq $true)
    } catch {
        return $false
    }
}

function Sync-DaGitHub {
    # Before starting, aligns this folder with the latest version on
    # GitHub. This PC never edits code locally: it only follows along. If
    # something goes wrong (offline, git missing), this does NOT block
    # startup: it launches as-is.
    param([string]$Repo)

    # Errors inside this function are non-terminating, on purpose. The top
    # of the file sets $ErrorActionPreference = "Stop": with that active,
    # capturing an external command's stderr via `2>&1` would turn any
    # ordinary line git writes into a terminating error — and git writes
    # there even on success. The `try` around this function's call would
    # then catch it and skip the update, defeating the very thing it's
    # meant to protect. This assignment only applies inside this function;
    # `Salva-IlRegistroImparato` resets it to "Stop" on its own.
    $ErrorActionPreference = "Continue"

    # If the comparator is already running, its sources aren't touched. A
    # second double-click during a recompute would change files under a
    # live process: `versione_del_codice`'s own guard notices and stops the
    # pipeline with an Italian message — so nothing gets corrupted — but the
    # run is lost, along with any AI answers already paid for.
    #
    # The sync is skipped and startup proceeds: `app/launcher.py` will find
    # the running server and reopen the window on it, which is what the
    # double-click was for. The update happens at the next startup with the
    # program closed.
    #
    # Port 8765 is checked because it's the first the launcher tries and the
    # one the store uses. If the program had ended up on a later port that
    # day, this guard simply doesn't trigger and behavior falls back to what
    # it was before this check existed: nothing worse is lost than before.
    if (Test-ComparatoreAcceso -Porta 8765) {
        Write-Host "[SYNC] Il comparatore e' gia' acceso: non tocco i file mentre gira." -ForegroundColor Yellow
        Write-Host "       L'aggiornamento si fara' al prossimo avvio a programma chiuso." -ForegroundColor Yellow
        return
    }

    # No on-screen prompt, ever. The repository is private: the day saved
    # credentials expire, git asks to redo them — and on Windows that means
    # Git Credential Manager opening a window. There's no one to answer it:
    # this runs on the morning double-click, and the window would sit open
    # before the program even starts, looking like the comparator failed to
    # launch. With these three settings git gives up immediately instead,
    # and startup proceeds on the local version — exactly what already
    # happens when the network doesn't respond.
    #
    # This is how git behaves whenever credentials stop being enough, not
    # something specific to one machine, and preventing it costs three
    # lines.
    #
    # `GIT_TERMINAL_PROMPT` stays set for the rest of the process, so it
    # also applies to the `git` call `versione_del_codice` makes later. That
    # is intentional: a prompt there would be just as out of place.
    $env:GIT_TERMINAL_PROMPT = '0'
    # Applied only to the two calls that talk to GitHub. The others —
    # status, rev-parse, checkout, reset, log — stay on disk and have
    # nothing to authenticate.
    $senzaDomande = @('-c', 'credential.interactive=false', '-c', 'core.askPass=')

    $git = Trova-Git
    if (-not $git) {
        Write-Host "[SYNC] git non trovato: avvio la versione locale." -ForegroundColor Yellow
        return
    }

    Write-Host "[SYNC] Controllo GitHub..." -ForegroundColor Gray
    # `2>&1`, not `2>$null`: the reason an update fails on a given day —
    # expired credentials, a diverged repository, a full disk — needs to be
    # recorded somewhere, since the message below doesn't state it. It's
    # kept aside and printed only if the command failed, so a successful
    # startup stays as clean as before.
    $erroreDelFetch = & $git @senzaDomande -C $Repo fetch --quiet --prune origin 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[SYNC] Nessuna connessione a GitHub: avvio la versione locale." -ForegroundColor Yellow
        Mostra-IlMotivo $erroreDelFetch
        return
    }

    # The canonical branch is GitHub's default: whichever it is, this
    # follows it automatically, with no branch name hardcoded.
    & $git @senzaDomande -C $Repo remote set-head origin --auto 2>$null | Out-Null
    $def = (& $git -C $Repo rev-parse --abbrev-ref origin/HEAD 2>$null)
    if (-not $def) { $def = "origin/main" }
    $ramo = $def -replace '^origin/', ''

    # The adapter registry is the only file the program rewrites itself
    # while still under git: when it learns a new supplier, or someone
    # confirms a changed schema, the entry lands in references\adapters.json.
    # The `reset --hard` below reverts tracked files to their GitHub state,
    # which overwrites it.
    #
    # The sync is NOT blocked for this: blocking it would mean the first
    # learned adapter disables updates permanently. A copy of what's about
    # to be reverted is kept instead, and reported.
    #
    # The copy is only made when there's actually something to lose — i.e.
    # when the file differs from GitHub. Doing it on every startup would let
    # a second startup overwrite the good copy with the one already reset by
    # the first.
    $copiaRegistro = $null
    $registroSporco = & $git -C $Repo status --porcelain -- "references/adapters.json" 2>$null
    if ($registroSporco) {
        $copiaRegistro = Salva-IlRegistroImparato -Repo $Repo
    }

    $prima = (& $git -C $Repo rev-parse HEAD 2>$null)
    # Aligns the folder with the default branch. Working data (app/data) is
    # outside git, so this does NOT touch state, confirmations, orders, or
    # AI memory.
    $erroreDelCheckout = & $git -C $Repo checkout -B $ramo --quiet "origin/$ramo" 2>&1
    # The checkout's own result is not itself checked here: $LASTEXITCODE
    # is only checked after the reset, the next command. A failed checkout
    # doesn't move content on disk — the reset points at "origin/$ramo", an
    # explicit ref — but it does leave the local branch with the wrong
    # name, silently.
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[SYNC] Non sono riuscito a mettermi sul ramo ${ramo}: avvio la versione locale." -ForegroundColor Yellow
        Mostra-IlMotivo $erroreDelCheckout
        return
    }
    $erroreDelReset = & $git -C $Repo reset --hard --quiet "origin/$ramo" 2>&1
    if ($LASTEXITCODE -ne 0) {
        # Saying "starting the local version" here would be wrong: the
        # `checkout -B` above already succeeded, so the branch and files on
        # disk are already GitHub's. The reset's job was clearing any
        # remaining local changes, and if it fails those changes are what's
        # left — not the previous version. Misstating that sends
        # troubleshooting in the wrong direction.
        Write-Host "[SYNC] Il codice e' quello nuovo, ma qualcosa sul disco non si e' lasciato ripulire." -ForegroundColor Yellow
        Mostra-IlMotivo $erroreDelReset
        return
    }
    $dopo = (& $git -C $Repo rev-parse HEAD 2>$null)

    if ($prima -eq $dopo) {
        Write-Host "[SYNC] Gia' all'ultima versione ($ramo)." -ForegroundColor Green
    } else {
        Write-Host "[SYNC] Aggiornato all'ultima versione su GitHub ($ramo):" -ForegroundColor Green
        & $git -C $Repo --no-pager log --oneline "$prima..$dopo" 2>$null | ForEach-Object { Write-Host "        $_" }
    }

    if ($copiaRegistro) {
        Write-Host "[SYNC] Gli adattatori imparati qui sono stati riportati alla versione di Daniele." -ForegroundColor Yellow
        Write-Host "       La copia di prima e' in $copiaRegistro" -ForegroundColor Yellow
    }
}

try {
    Sync-DaGitHub -Repo $skillDir
} catch {
    Write-Host "[SYNC] Salto l'aggiornamento ($($_.Exception.Message)): avvio la versione locale." -ForegroundColor Yellow
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @(),
        [switch]$IsCommand
    )

    if ($IsCommand) {
        if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) {
            return $false
        }
    } elseif (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $false
    }

    try {
        # `openpyxl` is part of the requirement, not an extra. Without it
        # the program starts and then dies on the first Excel price list,
        # with an error that never names the missing library: it looks like
        # a comparator failure. Better to reject an incomplete Python here
        # and try the next candidate. The macOS probe (`comune.sh`) already
        # checked for both; this one didn't, for no good reason.
        & $Executable @PrefixArguments -c "import sys, openpyxl; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    Write-Host ""
    Write-Host "[ERRORE] File di avvio non trovato:" -ForegroundColor Red
    Write-Host "         $launcherPath"
    Read-Host "Premi INVIO per chiudere"
    exit 1
}

$candidates = @(
    [pscustomobject]@{
        Executable = (Join-Path $skillDir "app\runtime\python.exe")
        Arguments = @()
        IsCommand = $false
        Label = "runtime portabile dell'app"
    },
    # The two places a Python installed on purpose on Windows ends up: for
    # all users, and for the current user only. These come BEFORE the two
    # Codex runtimes, and the reason is recorded here so it isn't lost.
    #
    # The store PC once ran on Codex's cache Python, which belongs to a
    # different program: that Codex folder gets rebuilt from scratch on
    # update, and stale leftovers from consecutive days had already been
    # seen sitting next to it, replaced twice within two days. The day it
    # changes name, the comparator fails to start — silently, possibly on
    # an order morning — with no fallback: no other Python existed on that
    # PC, `py` wasn't installed, and the `python` on PATH was the Microsoft
    # Store's 0-byte placeholder.
    #
    # If nothing is installed here, this block simply finds nothing and
    # changes nothing: it falls through to Codex as before. The day someone
    # installs Python, the program stops depending on a host it doesn't own.
    #
    # The version is hardcoded. Moving to 3.14 means adding a pair of lines
    # above: intentionally explicit rather than pattern-matched, since
    # nothing tests this file and Mac-based development has no PowerShell
    # to catch a mistake here.
    [pscustomobject]@{
        Executable = (Join-Path $env:ProgramFiles "Python313\python.exe")
        Arguments = @()
        IsCommand = $false
        Label = "Python 3.13 installato sul PC del negozio"
    },
    [pscustomobject]@{
        Executable = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
        Arguments = @()
        IsCommand = $false
        Label = "Python 3.13 installato per questo utente"
    },
    [pscustomobject]@{
        Executable = (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
        Arguments = @()
        IsCommand = $false
        Label = "runtime Python incluso in Codex"
    },
    [pscustomobject]@{
        Executable = (Join-Path $env:LOCALAPPDATA "OpenAI\Codex\runtimes\python\python.exe")
        Arguments = @()
        IsCommand = $false
        Label = "runtime Python Codex in AppData"
    },
    [pscustomobject]@{
        Executable = "py"
        Arguments = @("-3")
        IsCommand = $true
        Label = "Python Launcher di Windows"
    },
    [pscustomobject]@{
        Executable = "python"
        Arguments = @()
        IsCommand = $true
        Label = "python nel PATH"
    }
)

$selected = $null
foreach ($candidate in $candidates) {
    if (Test-PythonCandidate -Executable $candidate.Executable -PrefixArguments $candidate.Arguments -IsCommand:$candidate.IsCommand) {
        $selected = $candidate
        break
    }
}

if ($null -eq $selected) {
    Write-Host ""
    Write-Host "[ERRORE] Non trovo un Python 3.10 o successivo che abbia openpyxl." -ForegroundColor Red
    Write-Host "         Ho controllato il runtime locale, i Python installati su questo"
    Write-Host "         computer, quelli inclusi in Codex, il comando py e il comando python."
    Write-Host "         Serve tutt'e due le cose: senza openpyxl il programma non legge i listini."
    Write-Host ""
    Write-Host "         Se un Python c'e' ma gli manca la libreria, si aggiunge cosi':"
    Write-Host "           <percorso di python.exe> -m pip install openpyxl"
    Write-Host ""
    Write-Host "         Non è stata eseguita alcuna installazione."
    Write-Host ""
    Read-Host "Premi INVIO per chiudere"
    exit 1
}

Write-Host "[OK] Uso $($selected.Label):" -ForegroundColor Green
Write-Host "     $($selected.Executable) $($selected.Arguments -join ' ')"

$pythonArguments = @($selected.Arguments) + @($launcherPath) + @($args)
& $selected.Executable @pythonArguments
$launchResult = $LASTEXITCODE

if ($launchResult -ne 0) {
    Write-Host ""
    Write-Host "[ERRORE] Il comparatore non è stato avviato." -ForegroundColor Red
    Write-Host "         Leggi il messaggio precedente per il dettaglio."
    Write-Host ""
    Read-Host "Premi INVIO per chiudere"
}

exit $launchResult
