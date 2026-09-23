"""La scrittura in posizione dentro un `.xlsx`: `scripts/lib/xlsx_in_posizione.mjs`.

Dal 5 settembre 2026 le copie dei listini non le ricostruisce piu' una libreria:
si apre lo ZIP, si cambia il testo delle sole celle della colonna d'ordine
dentro l'XML del foglio, e si richiude copiando ogni altra parte byte per byte.

Qui si prova il modulo da solo, su fogli scritti a mano, e poi la compilazione
**su tutti i listini `.xlsx` del repository**: e' la prova che Daniele ha
chiesto — «va fatto bene e controllato con test di compilazione su piu'
listini» — e su ogni listino pretende quattro cose: la guardia cella per cella
passa, openpyxl riapre la copia, ogni parte dello ZIP tranne il foglio e'
identica all'originale, e LibreOffice la converte senza errori dove c'e'.
L'ultima prova — aprire tre copie con Excel, cambiarle, salvarle — la fa una
persona, e non e' sostituibile.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import copia_fedele  # noqa: E402
from test_writer_ordini import listino_con_stringhe_vuote, node_disponibile, xlsx_a_mano  # noqa: E402

MODULO = SKILL_ROOT / "scripts" / "lib" / "xlsx_in_posizione.mjs"
WRITER = SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"

# Il banco che parla col modulo: legge un JSON di comandi, li esegue e risponde
# in JSON.  Sta qui e non in un file `.mjs` di prova perche' e' l'unico posto in
# cui serve.
_BANCO = r"""
import { apriLibro } from PERCORSO_MODULO;
const [sorgente, uscita, comandi] = process.argv.slice(2);
const risposta = {};
try {
  const libro = await apriLibro(sorgente);
  for (const comando of JSON.parse(comandi)) {
    const foglio = libro.foglio(comando.foglio ?? "FIRST");
    if (comando.fai === "valore") risposta[comando.cella] = foglio.valore(comando.cella);
    else if (comando.fai === "scrivi") foglio.scriviNumero(comando.cella, comando.numero);
    else if (comando.fai === "svuota") risposta.svuotate = foglio.svuota(comando.colonna, comando.da, comando.a);
    else if (comando.fai === "griglia") {
      const g = foglio.griglia();
      risposta.griglia = { righe: g.rowCount, colonne: g.columnCount };
    } else if (comando.fai === "prefisso") risposta.prefisso = foglio.prefisso;
  }
  if (uscita) risposta.sostituite = await libro.salva(uscita);
} catch (errore) {
  risposta.errore = String(errore?.message ?? errore);
}
console.log(JSON.stringify(risposta));
"""


def _parti(percorso: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(percorso) as archivio:
        return {nome: archivio.read(nome) for nome in archivio.namelist()}


def _parti_diverse(originale: Path, copia: Path) -> list[str]:
    prima, dopo = _parti(originale), _parti(copia)
    assert list(prima) == list(dopo), f"le parti non sono le stesse: {list(prima)} / {list(dopo)}"
    return [nome for nome in prima if prima[nome] != dopo[nome]]


def _xml_ben_formato(percorso: Path) -> None:
    for nome, contenuto in _parti(percorso).items():
        if nome.endswith(".xml") or nome.endswith(".rels"):
            ElementTree.fromstring(contenuto)


class BancoDelModulo(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile")

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.banco = self.cartella / "banco.mjs"
        self.banco.write_text(
            _BANCO.replace("PERCORSO_MODULO", json.dumps(MODULO.resolve().as_uri())), encoding="utf-8",
        )

    def esegui(self, sorgente: Path, comandi: list[dict[str, Any]], uscita: Path | None = None) -> dict[str, Any]:
        esito = subprocess.run(
            [str(self.node), str(self.banco), str(sorgente), str(uscita or ""), json.dumps(comandi)],
            capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
        )
        self.assertEqual(esito.returncode, 0, esito.stderr)
        return json.loads(esito.stdout.strip().splitlines()[-1])


class LaScritturaInPosizione(BancoDelModulo):
    """Sul listino scritto a mano di `test_writer_ordini`: A2 e A5 sono stringhe
    condivise vuote, C2 e C5 quantita' preesistenti, C4 un titolo di sezione."""

    def setUp(self) -> None:
        super().setUp()
        self.sorgente = listino_con_stringhe_vuote(self.cartella / "listino.xlsx")
        self.copia = self.cartella / "copia.xlsx"

    def test_legge_le_celle_come_le_vede_chi_apre_il_foglio(self) -> None:
        letto = self.esegui(self.sorgente, [
            {"fai": "valore", "cella": "A1"}, {"fai": "valore", "cella": "A2"},
            {"fai": "valore", "cella": "C2"}, {"fai": "valore", "cella": "C4"},
            {"fai": "valore", "cella": "C3"}, {"fai": "griglia"},
        ])
        self.assertEqual(letto["A1"], "EAN")
        self.assertEqual(letto["A2"], "", "la stringa condivisa vuota e' vuota, non il suo indice")
        self.assertEqual(letto["C2"], 3)
        self.assertEqual(letto["C4"], "SEZIONE SOLARI")
        self.assertIsNone(letto["C3"])
        self.assertEqual(letto["griglia"], {"righe": 5, "colonne": 3})

    def test_scrive_e_svuota_solo_quello_che_le_si_chiede(self) -> None:
        risposta = self.esegui(self.sorgente, [
            {"fai": "svuota", "colonna": "C", "da": 2, "a": 2},
            {"fai": "svuota", "colonna": "C", "da": 5, "a": 5},
            {"fai": "scrivi", "cella": "C3", "numero": 5},
        ], self.copia)
        self.assertEqual(risposta.get("errore"), None)
        self.assertEqual(risposta["svuotate"], 1)
        self.assertEqual(risposta["sostituite"], 1, "solo il foglio: niente formule, niente workbook.xml")
        self.assertEqual(_parti_diverse(self.sorgente, self.copia), ["xl/worksheets/sheet1.xml"])
        _xml_ben_formato(self.copia)
        libro = load_workbook(self.copia)
        foglio = libro["Listino"]
        self.assertEqual(foglio["C3"].value, 5)
        self.assertIsNone(foglio["C2"].value)
        self.assertIsNone(foglio["C5"].value)
        self.assertEqual(foglio["C4"].value, "SEZIONE SOLARI", "il titolo di sezione resta")
        self.assertIn(foglio["A2"].value, (None, ""))
        self.assertEqual(foglio["B2"].value, "PRODOTTO DUE")
        libro.close()
        esito = copia_fedele.confronta_copia(
            self.sorgente, self.copia, colonna_ordine="C", prima_riga=2, quantita={3: 5},
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_una_riga_che_non_c_e_viene_creata_al_suo_posto(self) -> None:
        self.esegui(self.sorgente, [{"fai": "scrivi", "cella": "C9", "numero": 2}], self.copia)
        _xml_ben_formato(self.copia)
        libro = load_workbook(self.copia)
        self.assertEqual(libro["Listino"]["C9"].value, 2)
        self.assertEqual(libro["Listino"].max_row, 9)
        libro.close()
        xml = _parti(self.copia)["xl/worksheets/sheet1.xml"].decode("utf-8")
        self.assertLess(xml.index('<row r="5">'), xml.index('<row r="9">'), "le righe restano in ordine")

    def test_il_listino_di_partenza_non_si_tocca(self) -> None:
        prima = hashlib.sha256(self.sorgente.read_bytes()).hexdigest()
        self.esegui(self.sorgente, [{"fai": "scrivi", "cella": "C3", "numero": 5}], self.copia)
        self.assertEqual(hashlib.sha256(self.sorgente.read_bytes()).hexdigest(), prima)


class LeFormeCheUnListinoVeroPuoAvere(BancoDelModulo):
    def foglio(self, corpo: str, nome: str = "listino.xlsx") -> Path:
        return xlsx_a_mano(self.cartella / nome, corpo)

    def test_una_cella_nuova_non_eredita_nessuno_stile(self) -> None:
        """Una cella esistente tiene il suo stile; una nuova non ne prende
        nessuno, nemmeno quello che `<cols>` dichiara per la colonna.

        ⚠ Su `documenti/prova2.xlsx` la colonna dichiara per tutte le 16.384
        colonne un formato «testo»: ereditarlo cambiava come si vede il numero,
        e la guardia cella per cella rifiutava la copia."""

        sorgente = self.foglio(
            '<cols><col min="3" max="3" style="7"/></cols>'
            '<sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>EAN</t></is></c></row>'
            '<row r="2"><c r="A2"><v>1</v></c><c r="C2" s="4"/></row>'
            '<row r="3"><c r="A3"><v>2</v></c></row>'
            '</sheetData>'
        )
        copia = self.cartella / "copia.xlsx"
        self.esegui(sorgente, [
            {"fai": "scrivi", "cella": "C3", "numero": 5},
            {"fai": "scrivi", "cella": "C2", "numero": 6},
        ], copia)
        xml = _parti(copia)["xl/worksheets/sheet1.xml"].decode("utf-8")
        self.assertIn('<c r="C3"><v>5</v></c>', xml, "la cella nuova non prende lo stile 7 della colonna")
        self.assertIn('<c r="C2" s="4"><v>6</v></c>', xml, "una cella esistente tiene il suo stile")

    def test_la_dimensione_dichiarata_si_allarga_con_la_cella_scritta(self) -> None:
        sorgente = self.foglio(
            '<dimension ref="A1:B2"/>'
            '<sheetData><row r="1"><c r="A1"><v>1</v></c></row><row r="2"><c r="B2"><v>2</v></c></row></sheetData>'
        )
        copia = self.cartella / "copia.xlsx"
        self.esegui(sorgente, [{"fai": "scrivi", "cella": "D6", "numero": 5}], copia)
        xml = _parti(copia)["xl/worksheets/sheet1.xml"].decode("utf-8")
        self.assertIn('<dimension ref="A1:D6"/>', xml)
        _xml_ben_formato(copia)

    def test_una_formula_nella_colonna_d_ordine_ferma_e_lo_dice(self) -> None:
        sorgente = self.foglio(
            '<sheetData><row r="2"><c r="A2"><v>1</v></c><c r="C2"><f>SUM(A2)</f><v>1</v></c></row></sheetData>'
        )
        risposta = self.esegui(sorgente, [{"fai": "scrivi", "cella": "C2", "numero": 5}], self.cartella / "copia.xlsx")
        self.assertIn("formula", risposta["errore"])
        self.assertIn("C2", risposta["errore"])
        self.assertFalse((self.cartella / "copia.xlsx").exists())

    def test_con_le_formule_nel_foglio_si_chiede_a_excel_di_ricalcolare(self) -> None:
        """I totali del fornitore sommano la colonna d'ordine, e qui non si
        ricalcola niente: `fullCalcOnLoad` li fa rifare a Excel all'apertura."""

        sorgente = self.foglio(
            '<sheetData><row r="1"><c r="B1"><f>SUM(C:C)</f><v>0</v></c></row>'
            '<row r="2"><c r="A2"><v>1</v></c></row></sheetData>'
        )
        copia = self.cartella / "copia.xlsx"
        risposta = self.esegui(sorgente, [{"fai": "scrivi", "cella": "C2", "numero": 5}], copia)
        self.assertEqual(risposta["sostituite"], 2)
        libro = _parti(copia)["xl/workbook.xml"].decode("utf-8")
        self.assertIn('<calcPr fullCalcOnLoad="1"/>', libro)
        self.assertLess(libro.index("</sheets>"), libro.index("<calcPr"), "dopo i fogli, come vuole lo schema")
        _xml_ben_formato(copia)
        # E senza formule il libro non si tocca.
        senza = self.foglio('<sheetData><row r="2"><c r="A2"><v>1</v></c></row></sheetData>', "senza.xlsx")
        risposta = self.esegui(senza, [{"fai": "scrivi", "cella": "C2", "numero": 5}], copia)
        self.assertEqual(risposta["sostituite"], 1)

    def test_i_tag_con_il_prefisso_si_leggono_e_si_scrivono_con_lo_stesso_prefisso(self) -> None:
        percorso = self.cartella / "prefisso.xlsx"
        xlsx_a_mano(percorso, "")
        foglio = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<x:sheetData><x:row r="1"><x:c r="A1"><x:v>7</x:v></x:c></x:row></x:sheetData></x:worksheet>'
        )
        parti = _parti(percorso)
        parti["xl/worksheets/sheet1.xml"] = foglio.encode("utf-8")
        with zipfile.ZipFile(percorso, "w", zipfile.ZIP_DEFLATED) as archivio:
            for nome, contenuto in parti.items():
                archivio.writestr(nome, contenuto)
        copia = self.cartella / "copia.xlsx"
        risposta = self.esegui(percorso, [
            {"fai": "prefisso"}, {"fai": "valore", "cella": "A1"}, {"fai": "scrivi", "cella": "C1", "numero": 5},
        ], copia)
        self.assertEqual(risposta["prefisso"], "x:")
        self.assertEqual(risposta["A1"], 7)
        xml = _parti(copia)["xl/worksheets/sheet1.xml"].decode("utf-8")
        self.assertIn('<x:c r="C1"><x:v>5</x:v></x:c>', xml)
        libro = load_workbook(copia)
        self.assertEqual(libro.worksheets[0]["C1"].value, 5)
        libro.close()

    def test_un_file_che_non_e_un_documento_excel_lo_dice(self) -> None:
        finto = self.cartella / "finto.xlsx"
        finto.write_bytes(b"non sono uno zip")
        risposta = self.esegui(finto, [{"fai": "valore", "cella": "A1"}])
        self.assertIn("ZIP", risposta["errore"])


# ---------------------------------------------------------------------------
# La compilazione su tutti i listini veri del repository
# ---------------------------------------------------------------------------

# La colonna d'ordine dei listini che il registro conosce; per gli altri si
# prende la prima colonna vuota.
_COLONNA_D_ORDINE = {
    "LISTINO BETULLA": "C",
    "3listino_Cipresso": "G",
    "Listino3_": "G",
    "OFFERTE": "H",
}
# I canvass di LARICE: colonna D, con 641 titoli di sezione dentro.
_LARICE = ("28.1", "30.1", "31.1", "32.1", "Copia di 30.1")


def _listini_veri() -> list[Path]:
    cartelle = (SKILL_ROOT / "listini-storici", SKILL_ROOT / "documenti")
    return sorted(
        percorso for cartella in cartelle if cartella.is_dir()
        for percorso in cartella.glob("*.xlsx") if not percorso.name.startswith("~$")
    )


def _colonna_d_ordine(percorso: Path, foglio: Any) -> str:
    nome = percorso.stem
    for inizio, colonna in _COLONNA_D_ORDINE.items():
        if nome.startswith(inizio):
            return colonna
    if nome.startswith(_LARICE):
        return "D"
    return copia_fedele.lettera_di_colonna(foglio.max_column + 1)


def _righe_del_piano(foglio: Any, colonna: str) -> list[int]:
    """Tre righe con merce, sparse, dove nella colonna d'ordine non c'e' testo."""

    indice = copia_fedele.numero_di_colonna(colonna)
    candidate = []
    # In lettura veloce le celle vuote non sanno il proprio numero di riga:
    # lo si conta da qui.
    for numero, riga in enumerate(foglio.iter_rows(min_row=2), start=2):
        cella_ordine = riga[indice - 1].value if indice <= len(riga) else None
        if isinstance(cella_ordine, str) and cella_ordine.strip():
            continue
        if any(isinstance(cella.value, (str, int, float)) for cella in riga[:3]):
            candidate.append(numero)
    if len(candidate) < 3:
        return candidate
    return [candidate[1], candidate[len(candidate) // 2], candidate[-1]]


class LaCompilazioneSuiListiniVeri(unittest.TestCase):
    """Per ogni listino `.xlsx` del repository: si compila con il writer vero
    e si pretende che la copia regga le quattro prove."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile")
        cls.listini = _listini_veri()
        if not cls.listini:
            raise unittest.SkipTest("Nessun listino .xlsx nel repository")
        cls.soffice = shutil.which("soffice")

    def compila(self, sorgente: Path, cartella: Path) -> tuple[Path, str, dict[int, int]]:
        libro = load_workbook(sorgente, read_only=True, data_only=False)
        try:
            foglio = libro.worksheets[0]
            colonna = _colonna_d_ordine(sorgente, foglio)
            righe = _righe_del_piano(foglio, colonna)
        finally:
            libro.close()
        self.assertGreaterEqual(len(righe), 1, f"nessuna riga di merce trovata in {sorgente.name}")
        quantita = {riga: 2 + posizione for posizione, riga in enumerate(righe)}
        piano = cartella / "final_order_plan.json"
        piano.write_text(json.dumps({"orders": [
            {"supplier": "prova", "supplier_source_row": riga, "quantity": colli}
            for riga, colli in quantita.items()
        ]}), encoding="utf-8")
        config = cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {"prova": str(sorgente)},
            "supplier_write_rules": {"prova": {
                "sheet": "FIRST", "order_column": colonna, "data_start_row": 2,
                "source_sha256": hashlib.sha256(sorgente.read_bytes()).hexdigest(),
            }},
        }), encoding="utf-8")
        uscita = cartella / "uscita"
        esito = subprocess.run(
            [str(self.node), str(WRITER), "--plan", str(piano), "--config", str(config), "--output-dir", str(uscita)],
            cwd=SKILL_ROOT / "scripts", capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, check=False,
        )
        self.assertEqual(esito.returncode, 0, f"{sorgente.name}: {esito.stderr or esito.stdout}")
        copie = list(uscita.glob("ORDINE_*.xlsx"))
        self.assertEqual(len(copie), 1, f"{sorgente.name}: {copie}")
        return copie[0], colonna, quantita

    def test_ogni_listino_del_repository_si_compila_e_la_copia_regge(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        radice = Path(temporanea.name)
        for numero, sorgente in enumerate(self.listini):
            with self.subTest(listino=sorgente.name):
                cartella = radice / f"listino_{numero}"
                cartella.mkdir()
                copia, colonna, quantita = self.compila(sorgente, cartella)

                # 1. La guardia cella per cella.
                esito = copia_fedele.confronta_copia(
                    sorgente, copia, colonna_ordine=colonna, prima_riga=2, quantita=quantita,
                )
                self.assertTrue(esito.fedele, f"{sorgente.name}: {copia_fedele.frase_di_rifiuto('prova', esito)}")

                # 2. openpyxl riapre la copia e ci trova le quantita'.
                libro = load_workbook(copia, read_only=True)
                try:
                    foglio = libro.worksheets[0]
                    for riga, colli in quantita.items():
                        self.assertEqual(foglio[f"{colonna}{riga}"].value, colli, f"{sorgente.name} {colonna}{riga}")
                finally:
                    libro.close()

                # 3. Ogni parte dello ZIP e' intatta, tranne il foglio (e il
                #    libro, solo per dire a Excel di ricalcolare).
                diverse = _parti_diverse(sorgente, copia)
                self.assertTrue(
                    set(diverse) <= {"xl/worksheets/sheet1.xml", "xl/workbook.xml"} or len(diverse) <= 2,
                    f"{sorgente.name}: parti cambiate {diverse}",
                )
                self.assertTrue(all(nome.startswith("xl/worksheets/") or nome == "xl/workbook.xml" for nome in diverse),
                                f"{sorgente.name}: parti cambiate {diverse}")
                _xml_ben_formato(copia)

                # 4. LibreOffice la converte senza errori, dove c'e'.
                if self.soffice:
                    conversione = subprocess.run(
                        [self.soffice, "--headless", "--convert-to", "csv", "--outdir", str(cartella), str(copia)],
                        capture_output=True, text=True, timeout=180, check=False,
                        env={**os.environ, "HOME": str(cartella)},
                    )
                    prodotto = cartella / (copia.stem + ".csv")
                    self.assertTrue(
                        conversione.returncode == 0 and prodotto.is_file(),
                        f"{sorgente.name}: LibreOffice {conversione.returncode} {conversione.stderr[-400:]}",
                    )


if __name__ == "__main__":
    unittest.main()
