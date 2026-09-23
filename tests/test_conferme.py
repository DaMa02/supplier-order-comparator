#!/usr/bin/env python3
"""Il magazzino delle conferme: quello che l'utente ha già risposto.

Che cosa si prova qui, e perché è metà del lavoro. Questo modulo è una memoria
che sopravvive al ricalcolo settimanale, quindi i suoi difetti non si vedono il
giorno in cui nascono: si vedono il lunedì dopo, quando una conferma vale su
merce sbagliata oppure non vale più su merce giusta. Le prove sono scritte
sulle due direzioni dell'errore, che non hanno lo stesso prezzo:

* **perdere una conferma** costa un clic all'utente. È il guasto tollerabile.
* **applicarne una sbagliata** costa un ordine vero, e si ripete ogni settimana
  in silenzio. È il guasto che non deve essere possibile.

I dati delle prove non sono inventati: vengono dai due export veri sul disco al
15 agosto 2026 — il DOPLO che l'utente riconferma a mano, la DIXOR che compare
due volte nello stesso elenco, e i codici articolo Noce che sono cambiati
tutti insieme quando il listino è passato dal CSV del sito al loro `.xls`.

⚠ Nessuna prova legge `app/data/`: quei file il programma se li riscrive da
solo, e una suite che ci poggia sopra passa o fallisce a seconda dell'ultima
run. I valori veri stanno qui sotto come costanti.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "scripts"))
sys.path.insert(0, str(RADICE / "app"))

import conferme  # noqa: E402
from conferme import (  # noqa: E402
    MagazzinoConferme,
    MagazzinoNonUtilizzabile,
    impronta_articolo,
    impronta_prodotto,
)


# Il caso che ha originato il modulo: la riga del gestionale e la riga Noce
# che l'utente ha confermato a mano, e che ogni lunedì tornava a chiedere.
PRODOTTO_DOPLO = {
    "id": "product:539",
    "ean": "8009721709065",
    "name": "DOPLO PIATTI DESSER 25PZ",
    "sourceRow": 539,
    "lastUnitPrice": 1.24,
}
# Lo stesso articolo Noce visto dai due listini: a sinistra il CSV del sito
# (codice articolo vuoto), a destra il loro `.xls` (codice articolo pieno). Sono
# la stessa merce e hanno due impronte diverse: è il caso su cui si decide che
# cosa fa `cerca`.
OFFERTA_NOCE_CSV = {
    "supplierId": "noce",
    "ean": "8009721709065",
    "supplierCode": "",
    "description": "Doplo piatto frutta 17 cm 25 pz",
}
OFFERTA_NOCE_XLS = {
    "supplierId": "noce",
    "ean": "8009721709065",
    "supplierCode": "0000000004795",
    "description": "Doplo piatto frutta 17 cm 25 pz",
}
OFFERTA_LARICE = {
    "supplierId": "larice",
    "ean": "8009721709065",
    "supplierCode": "SM4412",
    "description": "DOPLO PIATTO FRUTTA CM.17 PZ.25",
}

ADESSO = "2026-08-15T09:30:00+00:00"
DOPO = "2026-08-22T09:30:00+00:00"
ANCORA_DOPO = "2026-08-29T09:30:00+00:00"


class BaseMagazzino(unittest.TestCase):
    """Un file nuovo per ogni prova, e la certezza che venga chiuso."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="conferme-"))
        self.percorso = self.cartella / "conferme.db"
        self.magazzino = self.apri()

    def tearDown(self) -> None:
        for magazzino in getattr(self, "_aperti", []):
            magazzino.chiudi()

    def apri(self, percorso: Path | None = None) -> MagazzinoConferme:
        magazzino = MagazzinoConferme(percorso or self.percorso)
        self._aperti = getattr(self, "_aperti", [])
        self._aperti.append(magazzino)
        return magazzino

    def articolo(self, prodotto: dict | None = None) -> str:
        return impronta_prodotto(prodotto or PRODOTTO_DOPLO)

    def conferma(
        self,
        *,
        offerta: dict | None = None,
        fornitore: str = "noce",
        accettata: bool = True,
        motivo: str = "Confermato a mano: è lo stesso piatto.",
        quando: str = ADESSO,
        magazzino: MagazzinoConferme | None = None,
    ) -> None:
        (magazzino or self.magazzino).ricorda(
            fornitore=fornitore,
            articolo=self.articolo(),
            offerta=impronta_articolo(offerta or OFFERTA_NOCE_XLS),
            accettata=accettata,
            motivo=motivo,
            quando=quando,
        )


class IdentitaDelProdotto(unittest.TestCase):
    """L'impronta dell'articolo del gestionale: che cosa ci entra e che cosa no."""

    def test_e_fatta_di_codice_a_barre_e_nome(self) -> None:
        self.assertEqual(
            impronta_prodotto(PRODOTTO_DOPLO),
            "8009721709065|DOPLO PIATTI DESSER 25PZ",
        )

    def test_la_riga_e_il_prezzo_non_ci_entrano(self) -> None:
        """La ragione per cui questo modulo esiste.

        Sui due export veri, 449 identificativi `product:<riga>` su 457 portano
        un articolo diverso da una settimana all'altra: se la riga entrasse
        nell'impronta, la conferma seguirebbe la posizione e non la merce.
        Il prezzo cambia ogni settimana sullo stesso articolo.
        """

        settimana_dopo = dict(PRODOTTO_DOPLO, sourceRow=17, lastUnitPrice=1.31, id="product:17")
        self.assertEqual(impronta_prodotto(PRODOTTO_DOPLO), impronta_prodotto(settimana_dopo))

    def test_un_codice_a_barre_riusato_non_eredita_la_conferma(self) -> None:
        """Il nome sta nell'impronta apposta: gli EAN qui sono scritti a mano.

        Stesso codice e merce diversa deve dare impronte diverse, altrimenti la
        conferma di ieri si aggancia da sola a un altro prodotto.
        """

        altro = dict(PRODOTTO_DOPLO, name="DOPLO BICCHIERI 200CC 25PZ")
        self.assertNotEqual(impronta_prodotto(PRODOTTO_DOPLO), impronta_prodotto(altro))

    def test_due_righe_dello_stesso_articolo_hanno_la_stessa_impronta(self) -> None:
        """Nell'export del 15 agosto la stessa DIXOR compare due volte.

        Sono lo stesso articolo: la conferma data sull'una vale sull'altra, e
        distinguerle vorrebbe dire chiedere due volte la stessa cosa.
        """

        prima = {"ean": "8019690600935", "name": "DIXOR POLVERE CLASSICO 40MISURINI", "sourceRow": 207}
        seconda = {"ean": "8019690600935", "name": "DIXOR POLVERE CLASSICO 40MISURINI", "sourceRow": 212}
        self.assertEqual(impronta_prodotto(prima), impronta_prodotto(seconda))

    def test_il_nome_si_normalizza_come_nel_resto_del_programma(self) -> None:
        sporco = {"ean": "8009721709065", "name": "  doplo   piatti-desser  25pz "}
        self.assertEqual(impronta_prodotto(sporco), impronta_prodotto(PRODOTTO_DOPLO))

    def test_senza_niente_da_identificare_non_ce_impronta(self) -> None:
        self.assertEqual(impronta_prodotto({}), "")
        self.assertEqual(impronta_prodotto({"ean": "", "name": "  "}), "")
        self.assertEqual(impronta_prodotto(None), "")

    def test_senza_codice_a_barre_resta_il_nome(self) -> None:
        """Sei prodotti dell'export vero non hanno EAN: non spariscono."""

        self.assertEqual(impronta_prodotto({"name": "SCOTEX ASCIUGATUTTO"}), "|SCOTEX ASCIUGATUTTO")


class ScritturaERilettura(BaseMagazzino):
    def test_quello_che_si_scrive_si_rilegge(self) -> None:
        self.conferma()
        trovata = self.magazzino.cerca("noce", self.articolo())
        self.assertIsNotNone(trovata)
        assert trovata is not None
        self.assertEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertTrue(trovata["accettata"])
        self.assertEqual(trovata["motivo"], "Confermato a mano: è lo stesso piatto.")
        self.assertEqual(trovata["valida_dal"], ADESSO)
        self.assertIsNone(trovata["valida_fino_a"])
        self.assertTrue(trovata["in_vigore"])

    def test_la_data_e_quella_del_chiamante(self) -> None:
        """Il magazzino non ha orologio: `quando` arriva da fuori e resta tale."""

        self.conferma(quando="2026-01-02T03:04:05+00:00")
        trovata = self.magazzino.cerca("noce", self.articolo())
        assert trovata is not None
        self.assertEqual(trovata["valida_dal"], "2026-01-02T03:04:05+00:00")

    def test_dentro_il_magazzino_non_si_chiama_nessun_orologio(self) -> None:
        """Difesa sul sorgente, perché è l'unico modo di provarne l'assenza.

        Una memoria che si data da sola non si può provare in modo ripetibile, e
        questo progetto ha già pagato quell'errore.
        """

        sorgente = (RADICE / "app" / "conferme.py").read_text(encoding="utf-8")
        for vietato in ("datetime.now", "utcnow", "date.today", "time.time", "time.monotonic"):
            self.assertNotIn(vietato, sorgente, f"il magazzino si data da solo con {vietato}")

    def test_un_no_e_una_memoria_come_le_altre(self) -> None:
        """«Non è lo stesso prodotto» detto una volta non deve tornare ogni lunedì."""

        self.conferma(accettata=False, motivo="Il 17 cm non è il dessert.")
        trovata = self.magazzino.cerca("noce", self.articolo())
        assert trovata is not None
        self.assertFalse(trovata["accettata"])

    def test_mai_visto_non_e_un_no(self) -> None:
        """`None` vuol dire «non ho memoria», e porta a una schermata diversa."""

        self.assertIsNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertIsNone(self.magazzino.cerca("noce", "9999999999999|PRODOTTO MAI VISTO"))
        self.conferma(accettata=False)
        risposta = self.magazzino.cerca("noce", self.articolo())
        self.assertIsNotNone(risposta)
        assert risposta is not None
        self.assertFalse(risposta["accettata"])

    def test_il_fornitore_non_dipende_dalle_maiuscole(self) -> None:
        """`NOCE` e `noce` sono lo stesso listino.

        Scrivere l'uno e cercare l'altro farebbe sparire la conferma senza un
        errore: somiglierebbe a «non l'avevo mai confermato».
        """

        self.conferma(fornitore="NOCE")
        self.assertIsNotNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertIsNotNone(self.magazzino.cerca("  Noce ", self.articolo()))

    def test_impronte_vuote_rifiutate(self) -> None:
        """Una conferma agganciata a `"|||"` tornerebbe su merce a caso."""

        buona = impronta_articolo(OFFERTA_NOCE_XLS)
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce", articolo="", offerta=buona, accettata=True, quando=ADESSO
            )
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce", articolo="|", offerta=buona, accettata=True, quando=ADESSO
            )
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce",
                articolo=self.articolo(),
                offerta=impronta_articolo({"supplierId": "noce"}),
                accettata=True,
                quando=ADESSO,
            )
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="", articolo=self.articolo(), offerta=buona, accettata=True, quando=ADESSO
            )
        self.assertEqual(self.magazzino.esporta(), [])

    def test_una_conferma_senza_data_non_si_scrive(self) -> None:
        """Senza `quando` non si può controllare un ordine sbagliato a posteriori."""

        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce",
                articolo=self.articolo(),
                offerta=impronta_articolo(OFFERTA_NOCE_XLS),
                accettata=True,
                quando="   ",
            )
        self.assertEqual(self.magazzino.esporta(), [])


class SostituzioneEStorico(BaseMagazzino):
    def test_la_risposta_nuova_sostituisce_quella_di_prima(self) -> None:
        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO, motivo="Riconfermato sul nuovo listino.")
        in_vigore = self.magazzino.cerca("noce", self.articolo())
        assert in_vigore is not None
        self.assertEqual(in_vigore["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertEqual(in_vigore["valida_dal"], DOPO)
        self.assertEqual(len(self.magazzino.tutte()), 1)

    def test_lo_storico_resta_e_dice_che_cosa_valeva_prima(self) -> None:
        """«Cosa avevo deciso prima, e quando» deve avere una risposta.

        A valle c'è un ordine vero: se una conferma sbagliata ha già comprato la
        merce sbagliata, la riga che lo spiega non deve essere stata sovrascritta.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO, motivo="Primo sì.")
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO, motivo="Secondo sì.")
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 2)
        vecchia, nuova = storia[0], storia[1]
        self.assertEqual(vecchia["offerta"], impronta_articolo(OFFERTA_NOCE_CSV))
        self.assertEqual(vecchia["motivo"], "Primo sì.")
        self.assertFalse(vecchia["in_vigore"])
        self.assertTrue(nuova["in_vigore"])

    def test_la_storia_non_ha_buchi(self) -> None:
        """La riga di prima si chiude nell'istante in cui la nuova entra in vigore.

        Se i due istanti non coincidessero, «che cosa valeva il 22 agosto»
        avrebbe due risposte oppure nessuna.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        self.conferma(offerta=OFFERTA_LARICE, quando=ANCORA_DOPO, fornitore="noce")
        storia = self.magazzino.esporta()
        self.assertEqual([voce["valida_dal"] for voce in storia], [ADESSO, DOPO, ANCORA_DOPO])
        self.assertEqual([voce["valida_fino_a"] for voce in storia], [DOPO, ANCORA_DOPO, None])

    def test_riconfermare_la_stessa_identica_risposta_non_scrive_niente(self) -> None:
        """Difesa contro il chiamante, non contro l'utente.

        La pagina si autosalva ogni 450 ms: se ogni salvataggio riscrivesse la
        conferma, una sola decisione diventerebbe centinaia di righe di storico
        che raccontano decisioni mai prese. E `valida_dal` resta il primo,
        perché è da allora che quella risposta vale.
        """

        self.conferma(quando=ADESSO)
        for _ in range(20):
            self.conferma(quando=DOPO)
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 1)
        self.assertEqual(storia[0]["valida_dal"], ADESSO)

    def test_cambiare_solo_il_motivo_e_un_cambiamento(self) -> None:
        """Il motivo è parte della risposta: è quello che si legge in un audit."""

        self.conferma(quando=ADESSO, motivo="Sì.")
        self.conferma(quando=DOPO, motivo="Sì, verificato sul catalogo cartaceo.")
        self.assertEqual(len(self.magazzino.esporta()), 2)

    def test_cambiare_solo_la_decisione_e_un_cambiamento(self) -> None:
        self.conferma(quando=ADESSO, accettata=True, motivo="")
        self.conferma(quando=DOPO, accettata=False, motivo="")
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 2)
        self.assertTrue(storia[0]["accettata"])
        self.assertFalse(storia[1]["accettata"])


class Dimenticare(BaseMagazzino):
    def test_toglie_la_conferma_in_vigore(self) -> None:
        self.conferma()
        self.assertTrue(self.magazzino.dimentica("noce", self.articolo(), quando=DOPO))
        self.assertIsNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertEqual(self.magazzino.tutte(), [])

    def test_la_riga_tolta_resta_nell_audit(self) -> None:
        """Una conferma sbagliata si toglie, ma non si fa sparire.

        Se ha già prodotto un ordine sbagliato, quella riga è l'unica cosa che
        lo spiega: cancellarla vorrebbe dire correggere il futuro e rendere
        illeggibile il passato.
        """

        self.conferma(motivo="Sì, sbagliando.")
        self.magazzino.dimentica("noce", self.articolo(), quando=DOPO)
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 1)
        self.assertEqual(storia[0]["motivo"], "Sì, sbagliando.")
        self.assertEqual(storia[0]["valida_fino_a"], DOPO)
        self.assertFalse(storia[0]["in_vigore"])

    def test_dimenticare_quello_che_non_ce_dice_di_no(self) -> None:
        self.assertFalse(self.magazzino.dimentica("noce", self.articolo()))
        self.conferma()
        self.assertTrue(self.magazzino.dimentica("noce", self.articolo()))
        self.assertFalse(self.magazzino.dimentica("noce", self.articolo()))

    def test_dopo_aver_dimenticato_si_puo_riconfermare(self) -> None:
        """La coppia torna libera: l'invariante non deve murare la porta."""

        self.conferma(quando=ADESSO)
        self.magazzino.dimentica("noce", self.articolo(), quando=DOPO)
        self.conferma(quando=ANCORA_DOPO, motivo="Ci riprovo.")
        in_vigore = self.magazzino.cerca("noce", self.articolo())
        assert in_vigore is not None
        self.assertEqual(in_vigore["valida_dal"], ANCORA_DOPO)
        self.assertEqual(len(self.magazzino.esporta()), 2)


class DueFornitori(BaseMagazzino):
    def test_lo_stesso_articolo_ha_una_risposta_per_fornitore(self) -> None:
        """Sì a Noce e no a Larice sullo stesso prodotto è normale."""

        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_XLS, accettata=True)
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, accettata=False, motivo="Loro hanno il 22 cm.")
        mega = self.magazzino.cerca("noce", self.articolo())
        larice = self.magazzino.cerca("larice", self.articolo())
        assert mega is not None and larice is not None
        self.assertTrue(mega["accettata"])
        self.assertFalse(larice["accettata"])
        self.assertEqual(len(self.magazzino.tutte()), 2)

    def test_dimenticare_uno_non_tocca_l_altro(self) -> None:
        self.conferma(fornitore="noce")
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE)
        self.magazzino.dimentica("noce", self.articolo(), quando=DOPO)
        self.assertIsNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertIsNotNone(self.magazzino.cerca("larice", self.articolo()))

    def test_sostituire_uno_non_tocca_l_altro(self) -> None:
        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, quando=ADESSO)
        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        larice = self.magazzino.cerca("larice", self.articolo())
        assert larice is not None
        self.assertEqual(larice["valida_dal"], ADESSO)
        self.assertEqual(len(self.magazzino.tutte()), 2)


class OffertaCambiata(BaseMagazzino):
    """La decisione su `cerca`, e il caso vero che la protegge."""

    def test_cerca_risponde_anche_se_l_articolo_del_fornitore_e_cambiato(self) -> None:
        """Il caso Noce del 15 agosto 2026.

        51 articoli identici — stesso codice a barre, stessa descrizione — hanno
        cambiato impronta perché il listino è passato dal CSV del sito al loro
        `.xls` e il codice articolo è passato da vuoto a pieno. Se `cerca` avesse
        preteso l'impronta di oggi e risposto «scaduta», quella settimana la
        memoria si sarebbe svuotata in blocco proprio mentre l'articolo era
        rimasto lo stesso: cioè il difetto che questo magazzino chiude.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV)
        trovata = self.magazzino.cerca("noce", self.articolo())
        self.assertIsNotNone(trovata)
        assert trovata is not None
        self.assertEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_CSV))
        self.assertNotEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))

    def test_il_chiamante_ha_in_mano_tutto_per_accorgersene(self) -> None:
        """La conferma non si applica alla cieca: chi chiama confronta.

        `cerca` restituisce l'impronta esatta che era stata confermata, così il
        confronto con quella di oggi è una riga sola e la differenza si può
        mostrare all'utente invece di ricominciare da zero.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV, motivo="Confermato sul CSV.")
        trovata = self.magazzino.cerca("noce", self.articolo())
        assert trovata is not None
        oggi = impronta_articolo(OFFERTA_NOCE_XLS)
        combacia = trovata["offerta"] == oggi
        self.assertFalse(combacia)
        self.assertEqual(trovata["motivo"], "Confermato sul CSV.")
        self.assertEqual(trovata["valida_dal"], ADESSO)


class ElencoEdEsportazione(BaseMagazzino):
    def test_tutte_mostra_solo_quello_che_vale_adesso(self) -> None:
        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, quando=DOPO)
        self.magazzino.dimentica("larice", self.articolo(), quando=ANCORA_DOPO)
        in_vigore = self.magazzino.tutte()
        self.assertEqual(len(in_vigore), 1)
        self.assertEqual(in_vigore[0]["fornitore"], "noce")
        self.assertEqual(in_vigore[0]["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertEqual(len(self.magazzino.esporta()), 3)

    def test_esporta_si_scrive_in_json(self) -> None:
        """Un `.db` non si legge a occhio: l'utente deve poter guardare."""

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        documento = json.dumps(self.magazzino.esporta(), ensure_ascii=False, indent=2)
        riletto = json.loads(documento)
        self.assertEqual(riletto, self.magazzino.esporta())
        self.assertIn("DOPLO PIATTI DESSER 25PZ", documento)
        self.assertIsInstance(riletto[0]["accettata"], bool)

    def test_esporta_e_ripetibile(self) -> None:
        """Due esportazioni dello stesso contenuto si possono confrontare."""

        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, quando=DOPO)
        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_XLS, quando=ADESSO)
        self.assertEqual(self.magazzino.esporta(), self.magazzino.esporta())
        self.assertEqual(
            [voce["fornitore"] for voce in self.magazzino.esporta()], ["larice", "noce"]
        )

    def test_due_risposte_nello_stesso_istante_restano_in_ordine(self) -> None:
        """Il caso in cui la sola data non basta a mettere in fila la storia.

        Non è teorico: se il chiamante data le conferme al secondo — come fa già
        il registro degli abbinamenti del progetto — due risposte date di
        seguito portano lo stesso `quando`, e la riga chiusa e quella nuova
        hanno lo stesso `valida_dal`. In un audit «prima» e «dopo» devono
        restare distinguibili.
        """

        self.conferma(quando=ADESSO, motivo="Primo sì.")
        self.conferma(quando=ADESSO, motivo="Secondo sì, corretto subito.")
        storia = self.magazzino.esporta()
        self.assertEqual([voce["motivo"] for voce in storia], ["Primo sì.", "Secondo sì, corretto subito."])
        self.assertEqual([voce["in_vigore"] for voce in storia], [False, True])
        self.assertEqual([voce["valida_dal"] for voce in storia], [ADESSO, ADESSO])

    def test_magazzino_vuoto(self) -> None:
        self.assertEqual(self.magazzino.tutte(), [])
        self.assertEqual(self.magazzino.esporta(), [])


class Persistenza(BaseMagazzino):
    def test_le_conferme_sopravvivono_alla_chiusura(self) -> None:
        """È tutto il punto: la memoria deve valere il lunedì dopo."""

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        self.magazzino.chiudi()

        riaperto = self.apri()
        trovata = riaperto.cerca("noce", self.articolo())
        assert trovata is not None
        self.assertEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertEqual(trovata["valida_dal"], DOPO)
        self.assertEqual(len(riaperto.esporta()), 2)

    def test_riaprire_non_azzera_e_non_duplica_lo_schema(self) -> None:
        self.conferma()
        for _ in range(3):
            self.apri().chiudi()
        riaperto = self.apri()
        self.assertEqual(len(riaperto.esporta()), 1)
        self.assertIsNotNone(riaperto.cerca("noce", self.articolo()))

    def test_il_giornale_e_wal(self) -> None:
        """Il servizio è multi-thread: senza WAL una lettura blocca una scrittura."""

        self.assertEqual(self.magazzino.giornale, "wal")

    def test_usare_il_magazzino_chiuso_lo_dice(self) -> None:
        self.conferma()
        self.magazzino.chiudi()
        self.magazzino.chiudi()  # due volte non fa danno
        with self.assertRaises(MagazzinoNonUtilizzabile):
            self.magazzino.cerca("noce", self.articolo())
        with self.assertRaises(MagazzinoNonUtilizzabile):
            self.magazzino.tutte()


class FileGuasto(unittest.TestCase):
    """Un file che non si apre non deve far esplodere il programma."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="conferme-guasto-"))

    def test_un_file_corrotto_da_un_errore_dichiarato(self) -> None:
        percorso = self.cartella / "conferme.db"
        percorso.write_bytes(b"questo non e' un database, sono 40 byte di spazzatura!!")
        with self.assertRaises(MagazzinoNonUtilizzabile) as caso:
            MagazzinoConferme(percorso)
        self.assertIn(str(percorso), str(caso.exception))

    def test_dopo_un_file_corrotto_il_programma_continua(self) -> None:
        """La forma che il servizio userà: si intercetta e si va avanti senza memoria."""

        rotto = self.cartella / "rotto.db"
        rotto.write_bytes(b"\x00\x01\x02 non sono SQLite \xff\xfe")
        magazzino = None
        try:
            magazzino = MagazzinoConferme(rotto)
        except MagazzinoNonUtilizzabile:
            magazzino = None
        self.assertIsNone(magazzino)

        buono = MagazzinoConferme(self.cartella / "buono.db")
        try:
            buono.ricorda(
                fornitore="noce",
                articolo=impronta_prodotto(PRODOTTO_DOPLO),
                offerta=impronta_articolo(OFFERTA_NOCE_XLS),
                accettata=True,
                quando=ADESSO,
            )
            self.assertIsNotNone(buono.cerca("noce", impronta_prodotto(PRODOTTO_DOPLO)))
        finally:
            buono.chiudi()

    def test_un_percorso_illeggibile_da_un_errore_dichiarato(self) -> None:
        """Il percorso è una cartella: capita con un percorso sbagliato in configurazione."""

        cartella = self.cartella / "sono-una-cartella"
        cartella.mkdir()
        with self.assertRaises(MagazzinoNonUtilizzabile):
            MagazzinoConferme(cartella)

    def test_un_file_di_una_versione_futura_non_si_tocca(self) -> None:
        """Un rollback del programma non deve riscrivere un file più nuovo di lui."""

        percorso = self.cartella / "futuro.db"
        conn = sqlite3.connect(str(percorso))
        conn.execute(f"PRAGMA user_version={conferme.VERSIONE_SCHEMA + 7}")
        conn.close()
        with self.assertRaises(MagazzinoNonUtilizzabile):
            MagazzinoConferme(percorso)

    def test_la_cartella_che_manca_si_crea(self) -> None:
        percorso = self.cartella / "mai" / "vista" / "conferme.db"
        magazzino = MagazzinoConferme(percorso)
        try:
            self.assertTrue(percorso.exists())
        finally:
            magazzino.chiudi()


class Concorrenza(unittest.TestCase):
    """Due che scrivono: il servizio ha il polling della pagina e il filo della pipeline."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="conferme-concorrenza-"))
        self.percorso = self.cartella / "conferme.db"
        self.articolo = impronta_prodotto(PRODOTTO_DOPLO)

    def _lancia(self, lavori: list) -> list[BaseException]:
        errori: list[BaseException] = []
        partenza = threading.Barrier(len(lavori))

        def esegui(lavoro) -> None:
            partenza.wait()
            try:
                lavoro()
            except BaseException as errore:  # noqa: BLE001 - va raccolto, non ingoiato
                errori.append(errore)

        fili = [threading.Thread(target=esegui, args=(lavoro,)) for lavoro in lavori]
        for filo in fili:
            filo.start()
        for filo in fili:
            filo.join(timeout=60)
        for filo in fili:
            self.assertFalse(filo.is_alive(), "un filo non è mai finito: scrittura bloccata")
        return errori

    def test_due_connessioni_scrivono_lo_stesso_articolo(self) -> None:
        """La coppia contesa: alla fine una sola risposta in vigore, e la storia intera.

        È il caso che rompe le scritture SQLite fatte male: due transazioni che
        cominciano in lettura e provano a diventare scritture si trovano già
        bloccate a vicenda e nessuna delle due può più riprovare.
        """

        primo = MagazzinoConferme(self.percorso)
        secondo = MagazzinoConferme(self.percorso)
        self.addCleanup(primo.chiudi)
        self.addCleanup(secondo.chiudi)

        def scrittura(magazzino: MagazzinoConferme, base: int):
            def lavoro() -> None:
                for passo in range(6):
                    magazzino.ricorda(
                        fornitore="noce",
                        articolo=self.articolo,
                        offerta=f"noce|8009721709065|COD{base + passo}|DOPLO PIATTO FRUTTA",
                        accettata=passo % 2 == 0,
                        motivo=f"giro {base + passo}",
                        quando=f"2026-08-15T10:{base + passo:02d}:00+00:00",
                    )
            return lavoro

        errori = self._lancia([scrittura(primo, 0), scrittura(secondo, 30)])
        self.assertEqual(errori, [], f"scrittura concorrente fallita: {errori}")

        in_vigore = primo.tutte()
        self.assertEqual(len(in_vigore), 1)
        storia = primo.esporta()
        self.assertEqual(len(storia), 12)
        # La catena si legge nell'ordine in cui le righe sono state scritte, non
        # in quello degli orologi: due fili non si mettono d'accordo sull'ora.
        per_scrittura = sorted(storia, key=lambda voce: voce["id"])
        for prima, dopo in zip(per_scrittura, per_scrittura[1:]):
            self.assertEqual(prima["valida_fino_a"], dopo["valida_dal"])
        self.assertIsNone(per_scrittura[-1]["valida_fino_a"])

    def test_due_connessioni_scrivono_articoli_diversi(self) -> None:
        primo = MagazzinoConferme(self.percorso)
        secondo = MagazzinoConferme(self.percorso)
        self.addCleanup(primo.chiudi)
        self.addCleanup(secondo.chiudi)

        def scrittura(magazzino: MagazzinoConferme, fornitore: str):
            def lavoro() -> None:
                for passo in range(15):
                    magazzino.ricorda(
                        fornitore=fornitore,
                        articolo=f"800130056{passo:04d}|ARTICOLO {passo}",
                        offerta=f"{fornitore}|800130056{passo:04d}||RIGA {passo}",
                        accettata=True,
                        quando=ADESSO,
                    )
            return lavoro

        errori = self._lancia([scrittura(primo, "noce"), scrittura(secondo, "larice")])
        self.assertEqual(errori, [], f"scrittura concorrente fallita: {errori}")
        self.assertEqual(len(primo.tutte()), 30)

    def test_lo_stesso_magazzino_da_piu_fili(self) -> None:
        """Un oggetto solo, condiviso: è come lo userà il servizio."""

        magazzino = MagazzinoConferme(self.percorso)
        self.addCleanup(magazzino.chiudi)

        def scrittura(base: int):
            def lavoro() -> None:
                for passo in range(12):
                    magazzino.ricorda(
                        fornitore="noce",
                        articolo=f"8001{base + passo:05d}|ARTICOLO {base + passo}",
                        offerta=f"noce|8001{base + passo:05d}||RIGA",
                        accettata=True,
                        quando=ADESSO,
                    )
                    magazzino.cerca("noce", f"8001{base + passo:05d}|ARTICOLO {base + passo}")
                    magazzino.tutte()
            return lavoro

        errori = self._lancia([scrittura(0), scrittura(100), scrittura(200)])
        self.assertEqual(errori, [], f"uso concorrente fallito: {errori}")
        self.assertEqual(len(magazzino.tutte()), 36)

    def test_il_database_impedisce_due_risposte_in_vigore(self) -> None:
        """L'invariante non è affidata al mio codice: la tiene l'indice unico.

        Se un giorno una transazione qui dentro sbaglia, il file non si riempie
        di due risposte contemporanee sulla stessa coppia: la scrittura fallisce.
        """

        magazzino = MagazzinoConferme(self.percorso)
        self.addCleanup(magazzino.chiudi)
        magazzino.ricorda(
            fornitore="noce",
            articolo=self.articolo,
            offerta=impronta_articolo(OFFERTA_NOCE_XLS),
            accettata=True,
            quando=ADESSO,
        )
        intruso = sqlite3.connect(str(self.percorso))
        self.addCleanup(intruso.close)
        with self.assertRaises(sqlite3.IntegrityError):
            intruso.execute(
                "INSERT INTO conferme (fornitore, articolo, offerta, accettata, motivo, "
                "valida_dal, valida_fino_a) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                ("noce", self.articolo, "noce|altro||ALTRO", 1, "", DOPO),
            )


# --- Le uguaglianze fra codici a barre --------------------------------------
#
# Il caso vero, misurato il 17 agosto 2026 sul confronto `2026-08-17_1746`:
# `LUXA SAPONE LIQ. EROG.250ML` sta nel gestionale col codice 4009428623194, che
# solo CIPRESSO usa (1,28 €/pz); NOCE, LARICE e BETULLA hanno lo stesso
# articolo sotto 8729721830575, a 1,15, 1,1625 e 1,19. Nessun punteggio puo'
# dedurlo: nel nome del gestionale la variante — ORIGINAL contro SETA — non c'e'
# affatto, e infatti la shortlist di BETULLA aveva la riga giusta **seconda**.
# Da qui in giu' si prova la memoria che chiude quel buco, e il prezzo dei suoi
# errori: un'uguaglianza sbagliata non rovina un prodotto presso un fornitore,
# li rovina tutti, e ogni settimana.

DOVE_GESTIONALE = "4009428623194"
DOVE_FORNITORI = "8729721830575"


class UguaglianzeFraCodiciTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self.cartella.cleanup)
        self.magazzino = MagazzinoConferme(Path(self.cartella.name) / "conferme.db")
        self.addCleanup(self.magazzino.chiudi)

    def test_due_codici_dichiarati_uguali_si_ritrovano_da_tutti_e_due_i_lati(self) -> None:
        self.assertIs(
            self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando=ADESSO), True
        )

        self.assertEqual(
            self.magazzino.classe(DOVE_GESTIONALE), [DOVE_GESTIONALE, DOVE_FORNITORI]
        )
        self.assertEqual(
            self.magazzino.classe(DOVE_FORNITORI), [DOVE_GESTIONALE, DOVE_FORNITORI]
        )

    def test_l_uguaglianza_non_ha_un_verso(self) -> None:
        """Dichiararla al contrario e' la stessa dichiarazione, e si toglie uguale.

        Senza l'ordine fisso l'indice unico non fermerebbe la seconda scrittura,
        e `separa` cercherebbe una coppia scritta nell'altro senso: cioe'
        un'uguaglianza sbagliata che non si riesce a togliere.
        """

        self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando=ADESSO)

        self.assertIs(self.magazzino.unisci(DOVE_FORNITORI, DOVE_GESTIONALE, quando=DOPO), False)
        self.assertEqual(len(self.magazzino.uguaglianze()), 1)
        self.assertIs(self.magazzino.separa(DOVE_FORNITORI, DOVE_GESTIONALE, quando=DOPO), True)
        self.assertEqual(self.magazzino.uguaglianze(), [])

    def test_le_dichiarazioni_si_incatenano(self) -> None:
        """A≡B e B≡C fanno un gruppo di tre: chi cerca A trova anche C."""

        self.magazzino.unisci("111", "222", quando=ADESSO)
        self.magazzino.unisci("222", "333", quando=DOPO)

        self.assertEqual(self.magazzino.classi(), [["111", "222", "333"]])
        self.assertEqual(self.magazzino.classe("333"), ["111", "222", "333"])

    def test_togliendo_l_anello_di_mezzo_restano_due_gruppi_separati(self) -> None:
        """Si scrive quello che qualcuno ha detto, non quello che se ne deduce.

        Se `unisci` avesse salvato anche A≡C — che nessuno ha mai dichiarato —
        togliere A≡B lascerebbe in piedi un'uguaglianza di cui nessuno saprebbe
        dire da dove è arrivata.
        """

        self.magazzino.unisci("111", "222", quando=ADESSO)
        self.magazzino.unisci("222", "333", quando=ADESSO)
        self.magazzino.separa("222", "333", quando=DOPO)

        self.assertEqual(self.magazzino.classi(), [["111", "222"]])
        self.assertEqual(self.magazzino.classe("333"), ["333"])

    def test_un_codice_senza_uguaglianze_e_se_stesso(self) -> None:
        """Chi interroga l'indice per EAN non deve avere un caso in meno."""

        self.assertEqual(self.magazzino.classe(DOVE_GESTIONALE), [DOVE_GESTIONALE])
        self.assertEqual(self.magazzino.classe(""), [])

    def test_lo_stesso_codice_scritto_in_modi_diversi_e_lo_stesso_codice(self) -> None:
        """Nel gestionale gli EAN sono scritti a mano, e il foglio li rilegge
        come numeri: senza questa riduzione un'uguaglianza dichiarata lunedi'
        non si ritroverebbe martedi', **senza dare nessun errore**."""

        self.magazzino.unisci(4009428623194.0, f"'{DOVE_FORNITORI}", quando=ADESSO)

        self.assertEqual(self.magazzino.classe(f"  {DOVE_GESTIONALE} "), [DOVE_GESTIONALE, DOVE_FORNITORI])
        self.assertEqual(conferme.codice_confrontabile("8729721830575.0"), DOVE_FORNITORI)

    def test_un_codice_solo_o_vuoto_non_e_una_dichiarazione(self) -> None:
        with self.assertRaises(ValueError):
            self.magazzino.unisci(DOVE_GESTIONALE, DOVE_GESTIONALE, quando=ADESSO)
        with self.assertRaises(ValueError):
            self.magazzino.unisci(DOVE_GESTIONALE, "", quando=ADESSO)
        with self.assertRaises(ValueError):
            self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando="")

    def test_niente_si_cancella_mai(self) -> None:
        """Se ha gia' prodotto un ordine sbagliato, la riga e' l'unica traccia
        che lo spiega: `separa` la chiude, non la butta."""

        self.magazzino.unisci(
            DOVE_GESTIONALE, DOVE_FORNITORI,
            articolo=f"{DOVE_GESTIONALE}|LUXA SAPONE LIQ EROG 250ML",
            offerta=f"noce|{DOVE_FORNITORI}||LUXA SAPONE EROGATORE ORIGINAL ML 250",
            motivo="scelta a mano dal listino NOCE, riga 4794",
            quando=ADESSO,
        )
        self.magazzino.separa(DOVE_GESTIONALE, DOVE_FORNITORI, quando=DOPO)

        storia = self.magazzino.esporta_uguaglianze()
        self.assertEqual(len(storia), 1)
        self.assertIs(storia[0]["in_vigore"], False)
        self.assertEqual(storia[0]["valida_dal"], ADESSO)
        self.assertEqual(storia[0]["valida_fino_a"], DOPO)
        self.assertIn("riga 4794", storia[0]["motivo"])
        self.assertIn("LUXA SAPONE EROGATORE", storia[0]["offerta"])

    def test_il_database_impedisce_due_dichiarazioni_contemporanee(self) -> None:
        """L'invariante la tiene l'indice unico, non il mio codice."""

        self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando=ADESSO)
        intruso = sqlite3.connect(str(self.magazzino.percorso))
        self.addCleanup(intruso.close)
        with self.assertRaises(sqlite3.IntegrityError):
            intruso.execute(
                "INSERT INTO uguaglianze (codice_a, codice_b, articolo, offerta, motivo, "
                "valida_dal, valida_fino_a) VALUES (?, ?, '', '', '', ?, NULL)",
                (DOVE_GESTIONALE, DOVE_FORNITORI, DOPO),
            )

    def test_le_classi_sono_stabili(self) -> None:
        """E' l'artefatto che la catena riceve: due letture dello stesso
        contenuto devono dare lo stesso documento, altrimenti confrontare la run
        di questa settimana con quella di prima non vuol dire niente."""

        self.magazzino.unisci("333", "111", quando=ADESSO)
        self.magazzino.unisci("222", "444", quando=ADESSO)

        self.assertEqual(self.magazzino.classi(), [["111", "333"], ["222", "444"]])
        self.assertEqual(self.magazzino.classi(), self.magazzino.classi())


if __name__ == "__main__":
    unittest.main()
