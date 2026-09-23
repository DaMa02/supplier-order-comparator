$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # La console continuerà con la codifica predefinita di Windows.
}

$skillDir = $PSScriptRoot
$launcherPath = Join-Path $skillDir "app\launcher.py"

function Mostra-IlMotivo {
    # Le righe che git ha scritto sullo standard error, stampate sotto la frase
    # in italiano che dice che cosa e' successo. Sono in inglese e sono rumore
    # su un PC dove nessuno guarda — ma sono l'unico modo di sapere perche' un
    # giorno l'aggiornamento non e' passato.
    param($Righe)

    foreach ($riga in @($Righe)) {
        $testo = "$riga".Trim()
        if ($testo) { Write-Host "        $testo" -ForegroundColor DarkYellow }
    }
}

function Trova-Git {
    # Il git di questo PC, o $null. Prima quello installato al suo posto
    # normale, poi quello nel PATH.
    $git = "C:\Program Files\Git\cmd\git.exe"
    if (Test-Path -LiteralPath $git) { return $git }
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Salva-IlRegistroImparato {
    # Mette da parte references\adapters.json prima che il reset lo riporti
    # indietro. Il nome porta la data e l'ora perche' due avvii ravvicinati non
    # si cancellino la copia a vicenda; si tengono le ultime dieci, che sono
    # meno di 200 KB in tutto.
    #
    # Se qualcosa qui non riesce, si avvisa e si va avanti: e' una rete di
    # sicurezza, non una precondizione per avviare il programma.
    param([string]$Repo)

    # Il `catch` qui sotto prende qualcosa solo se l'errore e' terminante, e a
    # renderlo tale e' questa preferenza. La funzione la dichiara da se' invece
    # di ereditarla dalla riga 1 del file, perche' `Sync-DaGitHub` — che e' chi
    # la chiama — la mette a "Continue" per potersi leggere gli errori di git.
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
    # Vero se su questa porta risponde IL comparatore, non un programma
    # qualunque che ha preso la porta.
    #
    # ⚠ La differenza e' tutta la guardia. `Get-NetTCPConnection` — che e'
    # quello che usava l'aggiornatore manuale — dice soltanto «qualcuno e' in
    # ascolto», e su quel «qualcuno» ci sta anche un programma che col
    # comparatore non c'entra niente. Fermare l'allineamento per lui vorrebbe
    # dire che il PC del negozio smette di ricevere aggiornamenti finche' quel
    # programma resta acceso: e' lo stesso difetto della guardia sull'albero
    # sporco, gia' scartata per questa ragione.
    #
    # `/api/health` risponde `{"ok": true, ...}` e non chiede nessun token: e'
    # la stessa rotta con cui `app/launcher.py` riconosce un server acceso.
    #
    # Nel dubbio si risponde «non e' acceso», cosi' l'allineamento si fa: se la
    # sonda sbaglia, si torna al comportamento che c'era prima di questa
    # guardia, che e' l'errore che costa meno.
    #
    # Si guarda in due passi, e il primo esiste per non pagare il secondo: la
    # mattina normale sulla 8765 non c'e' nessuno, e scoprirlo con una
    # richiesta HTTP vuol dire caricare lo stack di .NET a ogni avvio per
    # sentirsi dire di no. `Get-NetTCPConnection` risponde subito e senza
    # caricare niente; la domanda «chi sei» si fa solo se qualcuno c'e'
    # davvero.
    param([int]$Porta)

    try {
        $inAscolto = Get-NetTCPConnection -LocalPort $Porta -State Listen -ErrorAction SilentlyContinue
    } catch {
        # Il cmdlet non c'e' (Windows troppo vecchio, modulo assente): non e'
        # un errore che valga la pena raccontare, e la risposta prudente e'
        # «non e' acceso», cioe' allinea.
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
    # Prima di avviare, allinea questa cartella all'ultima versione su GitHub.
    # Questo PC non modifica il codice in locale: rincorre e basta. Se qualcosa
    # va storto (offline, git assente), NON blocca l'avvio: parte com'e'.
    param([string]$Repo)

    # ⚠ Dentro questa funzione l'errore non e' terminante, ed e' di proposito.
    # In cima al file c'e' $ErrorActionPreference = "Stop": con quello attivo,
    # raccogliere lo standard error di un comando esterno con `2>&1` trasforma
    # una riga qualunque scritta da git in un errore terminante — e git scrive
    # li' anche quando ha funzionato. Il `try` attorno alla chiamata a questa
    # funzione lo prenderebbe e salterebbe l'aggiornamento: cioe' il rimedio
    # spegnerebbe proprio la cosa che deve proteggere. L'assegnazione vale solo
    # qui dentro; `Salva-IlRegistroImparato` si rimette "Stop" da sola.
    $ErrorActionPreference = "Continue"

    # ⚠ Se il comparatore sta gia' girando, i suoi sorgenti non si toccano.
    # Un secondo doppio clic mentre e' in corso un ricalcolo cambierebbe i file
    # sotto un processo vivo: la guardia di `versione_del_codice` se ne accorge
    # e ferma la catena con una frase in italiano — quindi non si corrompe
    # niente — ma la run si perde, e con lei le domande gia' pagate all'AI.
    #
    # Si salta l'allineamento e si va avanti: `app/launcher.py` trovera' il
    # server acceso e riaprira' la finestra su quello, che e' quello che chi ha
    # fatto doppio clic voleva. L'aggiornamento si fara' al primo avvio a
    # programma chiuso.
    #
    # La porta e' la 8765 perche' e' la prima che il lanciatore prova ed e'
    # quella del negozio. Se quel giorno il programma fosse finito su una porta
    # piu' avanti, la guardia non scatta e si torna al comportamento di prima:
    # non si perde niente che non si perdesse gia'.
    if (Test-ComparatoreAcceso -Porta 8765) {
        Write-Host "[SYNC] Il comparatore e' gia' acceso: non tocco i file mentre gira." -ForegroundColor Yellow
        Write-Host "       L'aggiornamento si fara' al prossimo avvio a programma chiuso." -ForegroundColor Yellow
        return
    }

    # ⚠ Nessuna domanda a schermo, mai. Il repository e' privato: il giorno in
    # cui le credenziali salvate scadono, git chiede di rifarle — e su Windows
    # a chiederlo e' Git Credential Manager, che apre una finestra. Qui non c'e'
    # nessuno a rispondere: e' il doppio clic del mattino, e la finestra
    # resterebbe aperta prima ancora che il programma parta, con l'aria di un
    # comparatore che non si avvia. Con queste tre, git rinuncia subito e
    # l'avvio prosegue con la versione locale — cioe' esattamente quello che
    # gia' fa quando la rete non risponde.
    #
    # Non e' una cosa osservata su quel PC: e' il modo in cui git si comporta
    # quando le credenziali non bastano piu', e il costo di prevenirlo e' tre
    # righe.
    #
    # `GIT_TERMINAL_PROMPT` resta impostata per il resto del processo, quindi
    # vale anche per il `git` che `versione_del_codice` esegue piu' tardi. E'
    # voluto: li' una domanda a schermo sarebbe altrettanto fuori posto.
    $env:GIT_TERMINAL_PROMPT = '0'
    # Vanno sulle due sole chiamate che parlano con GitHub. Le altre — status,
    # rev-parse, checkout, reset, log — restano sul disco e non hanno nessuno
    # da autenticare.
    $senzaDomande = @('-c', 'credential.interactive=false', '-c', 'core.askPass=')

    $git = Trova-Git
    if (-not $git) {
        Write-Host "[SYNC] git non trovato: avvio la versione locale." -ForegroundColor Yellow
        return
    }

    Write-Host "[SYNC] Controllo GitHub..." -ForegroundColor Gray
    # `2>&1` e non `2>$null`: il motivo per cui un giorno l'aggiornamento non
    # passa — credenziali scadute, repository divergente, disco pieno — non
    # esisteva da nessuna parte, e la frase qui sotto non lo dice. Si tiene da
    # parte e si stampa solo se il comando e' fallito, cosi' l'avvio riuscito
    # resta pulito come prima.
    $erroreDelFetch = & $git @senzaDomande -C $Repo fetch --quiet --prune origin 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[SYNC] Nessuna connessione a GitHub: avvio la versione locale." -ForegroundColor Yellow
        Mostra-IlMotivo $erroreDelFetch
        return
    }

    # Il ramo canonico e' quello predefinito su GitHub: qualunque esso sia, si
    # segue da solo, senza nomi di rami scritti a mano.
    & $git @senzaDomande -C $Repo remote set-head origin --auto 2>$null | Out-Null
    $def = (& $git -C $Repo rev-parse --abbrev-ref origin/HEAD 2>$null)
    if (-not $def) { $def = "origin/main" }
    $ramo = $def -replace '^origin/', ''

    # ⚠ Il registro degli adattatori e' l'unico file che il programma si
    # riscrive da solo restando sotto git: quando impara un fornitore nuovo, o
    # quando qualcuno conferma uno schema variato, la voce finisce in
    # references\adapters.json. Il `reset --hard` qui sotto riporta i file
    # tracciati a com'erano su GitHub, quindi se ne mangia il contenuto.
    #
    # Qui NON si blocca l'allineamento: bloccarlo vorrebbe dire che il primo
    # adattatore imparato spegne gli aggiornamenti per sempre. Si tiene una
    # copia di quello che sta per essere riportato indietro, e lo si dice.
    #
    # La copia si fa solo quando c'e' davvero qualcosa da perdere — cioe'
    # quando il file risulta modificato rispetto a GitHub. Farla a ogni avvio
    # vorrebbe dire che il secondo avvio sovrascrive la copia buona con quella
    # gia' azzerata dal primo.
    $copiaRegistro = $null
    $registroSporco = & $git -C $Repo status --porcelain -- "references/adapters.json" 2>$null
    if ($registroSporco) {
        $copiaRegistro = Salva-IlRegistroImparato -Repo $Repo
    }

    $prima = (& $git -C $Repo rev-parse HEAD 2>$null)
    # Allineo la cartella al ramo predefinito. I dati di lavoro (app/data) sono
    # fuori da git, quindi questo NON tocca stato, conferme, ordini, memoria AI.
    $erroreDelCheckout = & $git -C $Repo checkout -B $ramo --quiet "origin/$ramo" 2>&1
    # L'esito del checkout non lo guardava nessuno: si controllava $LASTEXITCODE
    # solo dopo il reset, che e' il comando successivo. Un checkout fallito non
    # sposta il contenuto sul disco — il reset punta a "origin/$ramo", un
    # riferimento esplicito — ma lascia il ramo locale con un nome sbagliato, e
    # nessuno lo sapeva.
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[SYNC] Non sono riuscito a mettermi sul ramo ${ramo}: avvio la versione locale." -ForegroundColor Yellow
        Mostra-IlMotivo $erroreDelCheckout
        return
    }
    $erroreDelReset = & $git -C $Repo reset --hard --quiet "origin/$ramo" 2>&1
    if ($LASTEXITCODE -ne 0) {
        # ⚠ Qui c'era scritto «avvio la versione locale», e non era vero: il
        # `checkout -B` qui sopra e' gia' riuscito, quindi ramo e file sul disco
        # sono gia' quelli di GitHub. Il reset serviva a togliere di mezzo
        # eventuali modifiche locali rimaste, e a non riuscirci sono quelle a
        # restare — non la versione di prima. Dirlo storto manda a cercare un
        # problema dalla parte sbagliata.
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
        # ⚠ `openpyxl` fa parte della domanda, non e' un di piu'. Senza, il
        # programma parte e poi muore al primo listino Excel, con un errore che
        # non nomina mai la libreria che manca: sembra un guasto del
        # comparatore. Meglio scartare qui un Python monco e provare il
        # prossimo. La sonda del Mac (`.avvia-comune.sh`) chiedeva gia' tutte e
        # due le cose; questa no, e l'asimmetria non aveva nessuna ragione.
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
    # I due posti in cui finisce un Python installato apposta su Windows: per
    # tutti gli utenti, e per il solo utente. Vengono PRIMA dei due runtime di
    # Codex, e la ragione sta scritta qui perche' non si perda.
    #
    # Fino al 19 agosto 2026 il PC del negozio girava con il Python della cache
    # di Codex, che e' un altro programma: quella cartella Codex la rifa' da
    # capo quando si aggiorna, e accanto a lei c'erano gia' due avanzi datati 13
    # e 14 agosto. In due giorni era stata sostituita due volte. Il giorno in
    # cui cambia nome, il comparatore non parte — in silenzio, probabilmente il
    # mattino di un ordine — e non c'era nessun ripiego: su quel PC non esisteva
    # nessun altro Python, `py` non era installato, e il `python` del PATH era
    # il segnaposto da 0 byte del Microsoft Store.
    #
    # Se qui non c'e' niente, questo blocco non trova nulla e non cambia
    # assolutamente niente: si va avanti con Codex come prima. Il giorno in cui
    # qualcuno installa Python, il programma smette di essere ospite da solo.
    #
    # ⚠ La versione e' scritta a mano. Passando alla 3.14, si aggiunge una
    # coppia di righe qui sopra: e' voluto che sia esplicito invece che cercato
    # con un modello, perche' questo file non lo prova nessun collaudo e chi
    # sviluppa su Mac non ha PowerShell per accorgersi di un errore.
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
