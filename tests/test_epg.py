"""XMLTV-Auswertung und Herunterladen ohne fremde Quelle."""

from __future__ import annotations

from datetime import UTC, datetime
import gzip
from pathlib import Path
import threading
import tracemalloc

from aiohttp import web
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.tv_programm import epg

KOPF = '<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'


def _datei(pfad: Path, inhalt: str, gepackt: bool = False) -> str:
    daten = inhalt.encode()
    pfad.write_bytes(gzip.compress(daten) if gepackt else daten)
    return str(pfad)


def _sender(kennung: str, name: str) -> str:
    return f'<channel id="{kennung}"><display-name>{name}</display-name></channel>\n'


def _sendung(kennung: str, start: str, titel: str, extra: str = "") -> str:
    return f'<programme start="{start} +0100" stop="{start[:8]}235959 +0100" channel="{kennung}"><title>{titel}</title>{extra}</programme>\n'


@pytest.mark.parametrize("gepackt", [False, True])
def test_auswerten(tmp_path: Path, gepackt: bool) -> None:
    inhalt = (
        KOPF + _sender("zdf", "DE - ZDF") + _sender("ard", "Das Erste")
        + _sendung("zdf", "20261112201500", "Krimi", "<sub-title> Folge  1 </sub-title><desc>Ein\n  Fall</desc><category>Krimi</category>")
        + _sendung("zdf", "20261112180000", "Nachrichten")
        + _sendung("ard", "20261112201500", "Tatort")
        + "</tv>"
    )
    daten = epg.auswerten(_datei(tmp_path / "a.xml", inhalt, gepackt), ["zdf"])
    assert daten["alle_sender"] == ["Das Erste", "ZDF"]
    assert list(daten["programm"]) == ["ZDF"]
    erste, zweite = daten["programm"]["ZDF"]
    assert erste[2] == "Nachrichten"  # nach Beginn sortiert
    assert zweite[2:] == ["Krimi", "Folge 1", "Ein Fall", "Krimi"]
    assert zweite[0] == datetime(2026, 11, 12, 19, 15, tzinfo=UTC).timestamp()


def test_zeit_ohne_zeitzone_ist_utc() -> None:
    assert epg._zeit("20261112201500") == datetime(2026, 11, 12, 20, 15, tzinfo=UTC).timestamp()
    assert epg._zeit("20260712201500 +0200") == datetime(2026, 7, 12, 18, 15, tzinfo=UTC).timestamp()
    assert epg._zeit("kaputt") is None
    assert epg._zeit(None) is None


def test_kaputte_dateien(tmp_path: Path) -> None:
    with pytest.raises(epg.EpgFehler, match="kein gültiges XMLTV"):
        epg.auswerten(_datei(tmp_path / "a.xml", KOPF + "<channel"), None)
    with pytest.raises(epg.EpgFehler, match="keine Sender"):
        epg.auswerten(_datei(tmp_path / "b.xml", KOPF + "</tv>"), None)


def test_eigene_entitaeten_werden_abgelehnt(tmp_path: Path) -> None:
    bombe = '<?xml version="1.0"?><!DOCTYPE tv [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><tv><channel id="x"><display-name>&b;</display-name></channel></tv>'
    with pytest.raises(epg.EpgFehler, match="Entitäten"):
        epg.auswerten(_datei(tmp_path / "a.xml", bombe), None)


def test_texte_und_sendungen_sind_begrenzt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epg, "MAX_SENDUNGEN", 3)
    riesig = "x" * 2_000_000
    inhalt = KOPF + _sender("zdf", "ZDF") + _sendung("zdf", "20261112201500", riesig, f"<desc>{riesig}</desc>")
    inhalt += "".join(_sendung("zdf", f"2026111221{m:02d}00", "kurz") for m in range(10)) + "</tv>"
    pfad = _datei(tmp_path / "a.xml", inhalt)
    del inhalt
    tracemalloc.start()
    daten = epg.auswerten(pfad, ["ZDF"])
    _jetzt, spitze = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    eintraege = daten["programm"]["ZDF"]
    assert len(eintraege) == 3
    assert len(eintraege[0][2]) == epg.LAENGEN["title"]
    assert len(eintraege[0][4]) == epg.LAENGEN["desc"]
    assert spitze < 1_000_000  # der Text von 2 MB wird nie ganz gesammelt


def test_speicher_haengt_nicht_an_der_dateigroesse(tmp_path: Path) -> None:
    """Sendungen nicht gewählter Sender bleiben nicht im Speicher hängen."""
    zeilen = [KOPF, _sender("a", "A"), _sender("b", "B")]
    zeilen += [_sendung("b", "20261112201500", f"Sendung {n}") for n in range(60_000)]
    pfad = _datei(tmp_path / "gross.xml", "".join(zeilen) + "</tv>")
    tracemalloc.start()
    daten = epg.auswerten(pfad, ["A"])
    _jetzt, spitze = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert daten["programm"] == {}
    assert spitze < 2_000_000


def test_entpackte_menge_ist_begrenzt(tmp_path: Path) -> None:
    inhalt = KOPF + _sender("a", "A") + "<!--" + " " * 500_000 + "--></tv>"
    with pytest.raises(epg.EpgFehler, match="Entpackt größer"):
        epg.auswerten(_datei(tmp_path / "a.xml.gz", inhalt, gepackt=True), None, grenze=100_000)


@pytest.fixture
async def quelle(aiohttp_server):
    async def gross(_request: web.Request) -> web.StreamResponse:
        antwort = web.StreamResponse()
        await antwort.prepare(_request)
        for _ in range(50):
            await antwort.write(b"x" * 10_000)
        return antwort

    async def klein(_request: web.Request) -> web.Response:
        return web.Response(body=b"<tv/>")

    app = web.Application()
    app.router.add_get("/gross", gross)
    app.router.add_get("/klein", klein)
    return await aiohttp_server(app)


@pytest.mark.usefixtures("socket_enabled")
async def test_herunterladen(hass: HomeAssistant, quelle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    schleife = threading.current_thread()
    zugriffe: list[bool] = []
    echtes_open = open

    def beobachtet(*args, **kwargs):
        zugriffe.append(threading.current_thread() is schleife)
        return echtes_open(*args, **kwargs)

    monkeypatch.setattr(epg, "open", beobachtet, raising=False)
    sitzung = async_get_clientsession(hass)
    ziel = str(tmp_path / "epg.xml")

    assert await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/klein", ziel) == 5
    assert Path(ziel).read_bytes() == b"<tv/>"
    assert zugriffe == [False]  # geöffnet im Executor, nicht in der Ereignisschleife

    with pytest.raises(epg.EpgFehler, match="größer"):
        await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/gross", ziel, grenze=100_000)
    assert not Path(ziel + ".teil").exists()


def test_schreibweisen_teilen_ein_kontingent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epg, "MAX_SENDUNGEN", 4)
    namen = ["ZDF", "zdf", "Z-D-F", "z.d.f"]
    inhalt = KOPF + "".join(_sender(f"k{n}", name) for n, name in enumerate(namen))
    inhalt += "".join(_sendung(f"k{n}", f"2026111220{m:02d}00", "x") for n in range(4) for m in range(3)) + "</tv>"
    daten = epg.auswerten(_datei(tmp_path / "a.xml", inhalt), ["ZDF"])
    assert list(daten["programm"]) == ["ZDF"]
    assert len(daten["programm"]["ZDF"]) == 4


def test_zahl_der_sender_ist_begrenzt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epg, "MAX_SENDER", 10)
    inhalt = KOPF + "".join(_sender(f"k{n}", f"Sender {n}") for n in range(11)) + "</tv>"
    with pytest.raises(epg.EpgFehler, match="mehr als 10 Sender"):
        epg.auswerten(_datei(tmp_path / "a.xml", inhalt), None)


def test_gesamtzahl_der_sendungen_ist_begrenzt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epg, "MAX_GESAMT", 5)
    inhalt = KOPF + _sender("a", "A") + _sender("b", "B")
    inhalt += "".join(_sendung(k, f"2026111220{m:02d}00", "x") for k in "ab" for m in range(4)) + "</tv>"
    daten = epg.auswerten(_datei(tmp_path / "a.xml", inhalt), None)
    assert sum(len(e) for e in daten["programm"].values()) == 5
