"""The program detects when it is no longer the one currently running.

Covers the decision logic in `app/versione_del_codice.py`, focused on the two
failure modes that matter:

1. false positive (reporting a change when there isn't one) — this would
   block a good run every week, which is why an unreadable file doesn't
   count as evidence of change;
2. watching the wrong files — `references/adapters.json` changes on every
   learned schema, and including it in the snapshot would trigger the
   warning after every recompute.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import versione_del_codice  # noqa: E402


class LaFotografia(unittest.TestCase):
    def test_prende_il_codice_del_programma(self) -> None:
        scattata = versione_del_codice.fotografia()

        self.assertIn("app/pipeline_jobs.py", scattata)
        self.assertIn("app/server.py", scattata)
        self.assertIn("scripts/registro.py", scattata)
        self.assertIn("app/versione_del_codice.py", scattata)

    def test_non_prende_i_dati_ne_il_generato_ne_le_librerie_di_terzi(self) -> None:
        """Including a file that changes on its own would trigger the warning by itself.

        Tests the rule, not today's folder contents: `vendor`, `__pycache__`
        and `app/data` currently hold no `.py` file, so checking the real
        snapshot wouldn't distinguish anything and the test would look like
        it covers this case without actually covering it.
        """

        da_guardare = versione_del_codice._e_da_guardare
        self.assertFalse(da_guardare(SKILL_ROOT / "scripts" / "vendor" / "@oai" / "lib.py"))
        self.assertFalse(da_guardare(SKILL_ROOT / "app" / "__pycache__" / "server.cpython-312.py"))
        self.assertFalse(da_guardare(SKILL_ROOT / "app" / "data" / "current" / "finto.py"))
        self.assertTrue(da_guardare(SKILL_ROOT / "app" / "server.py"))
        self.assertTrue(da_guardare(SKILL_ROOT / "scripts" / "registro.py"))

        scattata = versione_del_codice.fotografia()
        self.assertEqual([nome for nome in scattata if not nome.endswith(".py")], [])
        self.assertEqual([nome for nome in scattata if nome.startswith("tests/")], [])

    def test_ogni_voce_ha_un_impronta_vera(self) -> None:
        scattata = versione_del_codice.fotografia()

        self.assertTrue(scattata)
        for nome, impronta in scattata.items():
            self.assertIsNotNone(impronta, nome)
            self.assertEqual(len(impronta or ""), 64, nome)

    def test_un_file_che_non_c_e_non_ha_impronta(self) -> None:
        self.assertIsNone(versione_del_codice._impronta(SKILL_ROOT / "questo_non_esiste.py"))


class QuandoIlCodiceCambia(unittest.TestCase):
    def riferimento(self) -> dict[str, str | None]:
        return dict(versione_del_codice.fotografia())

    def test_senza_cambiamenti_non_dice_niente(self) -> None:
        self.assertEqual(versione_del_codice.file_cambiati(self.riferimento()), [])
        self.assertIsNone(versione_del_codice.avviso_del_codice_cambiato(self.riferimento()))

    def test_un_file_diverso_viene_nominato(self) -> None:
        prima = self.riferimento()
        prima["scripts/registro.py"] = "0" * 64

        cambiati = versione_del_codice.file_cambiati(prima)

        self.assertEqual(cambiati, ["scripts/registro.py"])

    def test_un_file_comparso_dopo_l_avvio_conta(self) -> None:
        prima = self.riferimento()
        prima.pop("app/launcher.py")

        self.assertIn("app/launcher.py", versione_del_codice.file_cambiati(prima))

    def test_un_file_sparito_dopo_l_avvio_conta(self) -> None:
        prima = self.riferimento()
        prima["app/modulo_tolto.py"] = "a" * 64

        self.assertIn("app/modulo_tolto.py", versione_del_codice.file_cambiati(prima))

    def test_un_file_che_non_si_e_riusciti_a_leggere_non_e_una_prova(self) -> None:
        """Not knowing isn't knowing it changed: this would otherwise block a good run."""

        prima = self.riferimento()
        prima["app/server.py"] = None

        self.assertEqual(versione_del_codice.file_cambiati(prima), [])

    def test_l_avviso_dice_che_cosa_fare_e_che_non_si_perde_niente(self) -> None:
        prima = self.riferimento()
        prima["scripts/registro.py"] = "0" * 64

        avviso = versione_del_codice.avviso_del_codice_cambiato(prima) or ""

        self.assertIn("AVVIA_COMPARATORE.cmd", avviso)
        self.assertIn("Chiudi il comparatore", avviso)
        self.assertIn("Non perdi niente", avviso)
        self.assertIn("scripts/registro.py", avviso)

    def test_con_molti_file_cambiati_l_elenco_non_diventa_un_muro(self) -> None:
        prima = self.riferimento()
        for nome in list(prima)[:9]:
            prima[nome] = "0" * 64

        avviso = versione_del_codice.avviso_del_codice_cambiato(prima) or ""

        self.assertIn("e altri 4", avviso)

    def test_lo_stato_e_pronto_per_una_risposta_json(self) -> None:
        stato = versione_del_codice.stato()

        self.assertEqual(stato["codiceCambiatoDopoLAvvio"], bool(stato["fileCambiati"]))
        self.assertIsInstance(stato["fileCambiati"], list)
        self.assertIsInstance(stato["messaggio"], str)


class LaFotografiaDellAvvio(unittest.TestCase):
    def test_e_stata_scattata_all_import(self) -> None:
        """If taken late, it would snapshot the disk as it is afterward and see nothing."""

        self.assertTrue(versione_del_codice.ALL_AVVIO)
        self.assertIn("app/pipeline_jobs.py", versione_del_codice.ALL_AVVIO)

    def test_l_orchestratore_la_scatta_all_avvio_e_non_alla_prima_run(self) -> None:
        """The moment the disk is read matters.

        If `pipeline_jobs` imported this module inside the function, the
        snapshot would be taken when the user clicks the button — i.e. after
        the change — and would never see anything. Tested the way it's
        actually true: in a fresh process, the module must already be loaded
        right after the orchestrator is imported.
        """

        codice = (
            "import sys\n"
            f"sys.path[:0] = [{str(SKILL_ROOT / 'app')!r}, {str(SKILL_ROOT / 'scripts')!r}]\n"
            "import pipeline_jobs\n"
            "print('versione_del_codice' in sys.modules)\n"
        )
        esito = subprocess.run(
            [sys.executable, "-c", codice], capture_output=True, text=True, timeout=120,
        )

        self.assertEqual(esito.stdout.strip(), "True", esito.stderr)


class FirmaTests(unittest.TestCase):
    """The signature answers "is this the same program?" as a single string."""

    def test_la_stessa_fotografia_da_la_stessa_firma(self) -> None:
        fotografia = {"app/server.py": "aaa", "app/launcher.py": "bbb"}
        self.assertEqual(
            versione_del_codice.firma(fotografia),
            versione_del_codice.firma(dict(reversed(list(fotografia.items())))),
        )

    def test_un_file_cambiato_cambia_la_firma(self) -> None:
        prima = {"app/server.py": "aaa", "app/launcher.py": "bbb"}
        dopo = {"app/server.py": "aaa", "app/launcher.py": "CAMBIATO"}
        self.assertNotEqual(versione_del_codice.firma(prima), versione_del_codice.firma(dopo))

    def test_un_file_comparso_o_sparito_cambia_la_firma(self) -> None:
        base = {"app/server.py": "aaa"}
        self.assertNotEqual(
            versione_del_codice.firma(base),
            versione_del_codice.firma({**base, "app/nuovo.py": "ccc"}),
        )

    def test_illeggibile_non_si_confonde_con_assente_ne_con_leggibile(self) -> None:
        """`None` means "couldn't be read", not a value like any other.

        If unreadable collapsed into absent, two different programs could
        claim the same signature after a read error, and the launcher would
        reuse a server that isn't running these sources.
        """

        assente = versione_del_codice.firma({"app/server.py": "aaa"})
        illeggibile = versione_del_codice.firma({"app/server.py": "aaa", "app/x.py": None})
        leggibile = versione_del_codice.firma({"app/server.py": "aaa", "app/x.py": "?"})
        self.assertNotEqual(assente, illeggibile)
        self.assertNotEqual(illeggibile, leggibile)

    def test_senza_argomenti_e_la_firma_di_quello_che_sta_girando(self) -> None:
        self.assertEqual(
            versione_del_codice.firma(),
            versione_del_codice.firma(versione_del_codice.ALL_AVVIO),
        )

    def test_il_disco_e_il_processo_coincidono_se_nessuno_ha_toccato_niente(self) -> None:
        self.assertEqual(versione_del_codice.firma(), versione_del_codice.firma_del_disco())


class EseguibileGitTests(unittest.TestCase):
    """Fallback order: the absolute Git-for-Windows path, then `PATH`, then
    the bare command — which must not behave worse than before this fix.
    """

    def test_usa_il_percorso_assoluto_se_c_e(self) -> None:
        with tempfile.NamedTemporaryFile() as finto_git:
            with mock.patch.object(
                versione_del_codice, "_GIT_ASSOLUTO_WINDOWS", Path(finto_git.name)
            ):
                self.assertEqual(versione_del_codice._eseguibile_git(), finto_git.name)

    def test_ripiega_sul_path_se_il_percorso_assoluto_manca(self) -> None:
        percorso_inesistente = Path("/questo/percorso/non/esiste/git.exe")
        with (
            mock.patch.object(versione_del_codice, "_GIT_ASSOLUTO_WINDOWS", percorso_inesistente),
            mock.patch.object(versione_del_codice.shutil, "which", return_value="/usr/bin/git"),
        ):
            self.assertEqual(versione_del_codice._eseguibile_git(), "/usr/bin/git")

    def test_se_manca_tutto_il_ripiego_e_lo_stesso_comando_nudo_di_prima(self) -> None:
        """Worst case: neither the absolute path nor `PATH` finds git.

        Falls back to the bare `"git"` command, the same as before this fix.
        If this test returned anything else, the fallback would have made
        things worse than before.
        """

        percorso_inesistente = Path("/questo/percorso/non/esiste/git.exe")
        with (
            mock.patch.object(versione_del_codice, "_GIT_ASSOLUTO_WINDOWS", percorso_inesistente),
            mock.patch.object(versione_del_codice.shutil, "which", return_value=None),
        ):
            self.assertEqual(versione_del_codice._eseguibile_git(), "git")


class PubblicataTests(unittest.TestCase):
    """`pubblicata()` caches its result process-wide (`_data_pubblicata`,
    `_data_gia_cercata`): reset it before each test, or the second test in
    this class would read the first one's result.
    """

    def setUp(self) -> None:
        self._skill_root_originale = versione_del_codice.SKILL_ROOT
        versione_del_codice._data_pubblicata = None
        versione_del_codice._data_gia_cercata = False

    def tearDown(self) -> None:
        versione_del_codice.SKILL_ROOT = self._skill_root_originale
        versione_del_codice._data_pubblicata = None
        versione_del_codice._data_gia_cercata = False

    def test_in_un_repository_vero_torna_la_data_iso_dell_ultimo_commit(self) -> None:
        with tempfile.TemporaryDirectory() as cartella:
            repo = Path(cartella)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True, timeout=15)
            subprocess.run(
                [
                    "git", "-C", str(repo),
                    "-c", "user.email=prova@esempio.it", "-c", "user.name=Prova",
                    "commit", "--quiet", "--allow-empty", "-m", "commit di prova",
                ],
                check=True,
                timeout=15,
            )
            attesa = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%cI"],
                capture_output=True, text=True, check=True, timeout=15,
            ).stdout.strip()

            versione_del_codice.SKILL_ROOT = repo

            self.assertEqual(versione_del_codice.pubblicata(), attesa)

    def test_in_una_cartella_che_non_e_un_repository_torna_none_e_non_solleva(self) -> None:
        with tempfile.TemporaryDirectory() as cartella:
            versione_del_codice.SKILL_ROOT = Path(cartella)

            self.assertIsNone(versione_del_codice.pubblicata())

    def test_se_il_sottoprocesso_non_parte_torna_none(self) -> None:
        with mock.patch.object(
            versione_del_codice.subprocess, "run", side_effect=OSError("git non trovato")
        ):
            self.assertIsNone(versione_del_codice.pubblicata())


class RiusoDelServerTests(unittest.TestCase):
    """A healthy server isn't enough: it must be running these exact sources.

    Without this check, reopening the `.cmd` after an update could reuse the
    old server, mistaking a response for a restart.
    """

    def setUp(self) -> None:
        import launcher

        self.launcher = launcher

    def test_la_stessa_firma_si_riusa(self) -> None:
        self.assertTrue(
            self.launcher.e_lo_stesso_programma({"ok": True, "firmaDelCodice": "abc"}, "abc")
        )

    def test_una_firma_diversa_non_si_riusa(self) -> None:
        self.assertFalse(
            self.launcher.e_lo_stesso_programma({"ok": True, "firmaDelCodice": "abc"}, "xyz")
        )

    def test_un_server_senza_firma_e_vecchio_e_non_si_riusa(self) -> None:
        """A server predating this safeguard reports nothing at all, and the
        absence itself is the answer: it's definitely old.
        """

        self.assertFalse(self.launcher.e_lo_stesso_programma({"ok": True}, "abc"))
        self.assertFalse(
            self.launcher.e_lo_stesso_programma({"ok": True, "firmaDelCodice": ""}, "abc")
        )
        self.assertFalse(
            self.launcher.e_lo_stesso_programma({"ok": True, "firmaDelCodice": None}, "abc")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
