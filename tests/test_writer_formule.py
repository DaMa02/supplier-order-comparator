"""Una formula nella colonna d'ordine non deve arrivare al fornitore.

R7 della revisione del 6 settembre 2026.  Il foglio dice gia' di no alle
formule nelle celle in cui **scrive** — `scriviNumero` e `svuota` si fermano —
ma una formula su una riga che nessuno ordina non la toccava nessuno: restava
nella copia, il confronto cella per cella vedeva formula contro formula e non
trovava differenze, ed Excel all'apertura la calcolava.  Nell'ordine
consegnato al fornitore comparivano colli che nessuno ha chiesto.

Il caso vive solo nei file **generati da programma**: un `.xlsx` salvato da
Excel il risultato memorizzato ce l'ha sempre, e con quello dentro la
compilazione si fermava gia'.

⚠ Il listino di prova lo scrive openpyxl e poi gli si toglie il `<v></v>`
vuoto: openpyxl 3.1.5 la formula la scrive **con** un valore in cache vuoto,
che il writer legge come lo zero di una quantita' e che quindi lo fermava lo
stesso.  Senza quel ritocco questa prova passerebbe anche con il difetto
dentro.

In coda c'e' lo stesso buco nel `.xls` di Noce, che ha una storia diversa:
li' al fornitore non e' mai arrivato niente, perche' il controllo preventivo
prima di compilare guarda tutta la colonna.  Era `compila_ordine` da solo a
guardarne meta'.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Callable

from openpyxl import Workbook

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "tests"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import test_xls_reader as banco_xls  # noqa: E402
import test_xls_writer as banco_noce  # noqa: E402
import xls_writer  # noqa: E402
from test_writer_ordini import node_disponibile  # noqa: E402

WRITER = SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"

# Quello che openpyxl scrive per `=5`: la formula e il posto del risultato,
# vuoto.  Un gestionale che genera il listino il posto non lo mette proprio.
_FORMULA_DI_OPENPYXL = "<f>5</f><v></v>"


def listino_con_una_formula(percorso: Path, ritocca: Callable[[str], str]) -> Path:
    """Il listino di prova, tre righe.

    | riga | A (EAN)       | B (DESCRIZIONE) | C (ordine)                  |
    |------|---------------|-----------------|-----------------------------|
    | 1    | EAN           | DESCRIZIONE     | ORDINE                      |
    | 2    | 8000000000002 | PRODOTTO DUE    | `=5` — e il piano non la ordina |
    | 3    | 8000000000003 | PRODOTTO TRE    | 4 — la riga che il piano ordina |
    """

    libro = Workbook()
    foglio = libro.active
    foglio.title = "Listino"
    foglio.append(["EAN", "DESCRIZIONE", "ORDINE"])
    foglio.append(["8000000000002", "PRODOTTO DUE", "=5"])
    foglio.append(["8000000000003", "PRODOTTO TRE", 4])
    libro.save(percorso)
    libro.close()

    with zipfile.ZipFile(percorso) as contenitore:
        parti = {nome: contenitore.read(nome) for nome in contenitore.namelist()}
    foglio_xml = parti["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert _FORMULA_DI_OPENPYXL in foglio_xml, foglio_xml
    parti["xl/worksheets/sheet1.xml"] = ritocca(foglio_xml).encode("utf-8")
    with zipfile.ZipFile(percorso, "w", zipfile.ZIP_DEFLATED) as contenitore:
        for nome, contenuto in parti.items():
            contenitore.writestr(nome, contenuto)
    return percorso


class UnaFormulaNellaColonnaDOrdine(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile per il writer XLSX")

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.uscita = self.cartella / "uscita"

    def compila(self, ritocca: Callable[[str], str]) -> subprocess.CompletedProcess:
        sorgente = listino_con_una_formula(self.cartella / "listino.xlsx", ritocca)
        piano = self.cartella / "final_order_plan.json"
        piano.write_text(json.dumps({
            "orders": [{"supplier": "prova", "supplier_source_row": 3, "quantity": 7}],
        }), encoding="utf-8")
        config = self.cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {"prova": str(sorgente)},
            "supplier_write_rules": {"prova": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": hashlib.sha256(sorgente.read_bytes()).hexdigest(),
            }},
        }), encoding="utf-8")
        return subprocess.run(
            [
                str(self.node), str(WRITER),
                "--plan", str(piano),
                "--config", str(config),
                "--output-dir", str(self.uscita),
            ],
            cwd=SKILL_ROOT / "scripts",
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def pretendi_che_si_fermi(self, esito: subprocess.CompletedProcess) -> None:
        detto = esito.stderr + esito.stdout
        self.assertNotEqual(esito.returncode, 0, detto)
        self.assertIn("formula", detto)
        self.assertIn("C2", detto)
        self.assertIn("non viene creato", detto)
        # Nessuna copia sul disco: l'ordine non si consegna a meta'.
        prodotte = sorted(percorso.name for percorso in self.uscita.glob("*.xlsx")) if self.uscita.is_dir() else []
        self.assertEqual(prodotte, [], prodotte)

    def test_senza_il_valore_in_cache_e_su_una_riga_non_ordinata_ferma_lo_stesso(self) -> None:
        """Il difetto R7: la copia usciva con la formula dentro."""

        self.pretendi_che_si_fermi(self.compila(lambda xml: xml.replace(_FORMULA_DI_OPENPYXL, "<f>5</f>")))

    def test_con_il_valore_in_cache_si_fermava_gia(self) -> None:
        """Il caso che il difetto non aveva: qui la formula vale una quantita',
        l'azzeramento la prende in mano e il foglio dice di no.  Vale la pena
        fissarlo, perche' e' il confine fra i due comportamenti."""

        self.pretendi_che_si_fermi(self.compila(lambda xml: xml.replace(_FORMULA_DI_OPENPYXL, "<f>5</f><v>5</v>")))


class UnaFormulaNellaColonnaDOrdineDelXls(unittest.TestCase):
    """Lo stesso buco nel `.xls` di Noce, dove la copia la fa `app/xls_writer.py`.

    Al fornitore non ci e' mai arrivata: il controllo preventivo che
    `app/server.py` fa prima di compilare (`controlla_colonna_ordine`) tutta la
    colonna d'ordine la guardava gia', e con una formula dentro la compilazione
    non parte.  Ma `compila_ordine`, da solo, si fermava all'ultima riga che il
    piano tocca: una formula piu' sotto passava, l'azzeramento la saltava — non
    e' un numero a lunghezza fissa — e restava nella copia.  I due controlli
    adesso dicono la stessa cosa.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)

    def test_una_formula_sotto_l_ultima_riga_ordinata_ferma_la_compilazione(self) -> None:
        # La formula sta nella riga 4 della colonna d'ordine; il piano ordina
        # soltanto la riga 3.
        celle = banco_noce.foglio_di_prova(
            guasto=banco_xls.formula(3, banco_noce.COLONNA_ORDINE, banco_xls.formula_numero(9.0)),
        )
        origine = self.cartella / "listino.xls"
        origine.write_bytes(banco_xls.costruisci_xls(
            [("Foglio1", celle)], extra_globali=banco_noce.recalcid(),
        ))
        copia = self.cartella / "ordine.xls"

        with self.assertRaises(xls_writer.CompilazioneXlsError) as errore:
            xls_writer.compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)

        self.assertIn("una formula", str(errore.exception))
        self.assertIn("riga 4", str(errore.exception))
        self.assertFalse(copia.exists())


if __name__ == "__main__":
    unittest.main()
