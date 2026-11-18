"""Weiterleitungen der Quelle werden selbst verfolgt und geprüft (GHSA-m7cx-3q6v-c42v)."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import socket

from aiohttp import web
from aiohttp.abc import AbstractResolver
import pytest
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.tv_programm import epg
from custom_components.tv_programm.const import DEFAULT_URL, DEFAULT_URL_ERSATZ

OEFFENTLICH = ipaddress.ip_address("93.184.216.34")  # nur als Antwort der Attrappe, nie verbunden


class Bewacht:
    """Sitzung, die nur zur Testquelle verbindet: Ein Fehler im Code erreicht nie das echte Netz.

    ``umleiten`` schickt Anfragen an erfundene Namen zur Testquelle auf 127.0.0.1.
    """

    def __init__(self, echt, erlaubt: set[str], umleiten: frozenset[str] = frozenset()) -> None:
        self._echt = echt
        self._erlaubt = erlaubt
        self._umleiten = umleiten
        self.rechner: list[str] = []

    def get(self, url, **kwargs):
        url = URL(url)
        self.rechner.append(url.host)
        assert url.host in self._erlaubt, f"Verbindung zu {url.host} versucht"
        assert kwargs.get("allow_redirects") is False
        if url.host in self._umleiten:
            url = url.with_host("127.0.0.1")
        return self._echt.get(url, **kwargs)


class FalschesDns(AbstractResolver):
    """Attrappe für das DNS: Antworten je Name, auch wechselnde (Rebinding)."""

    def __init__(self, antworten: dict[str, list[str]]) -> None:
        self._antworten = antworten
        self.fragen: list[str] = []

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET):
        self.fragen.append(host)
        folge = self._antworten[host]
        adresse = folge.pop(0) if len(folge) > 1 else folge[0]
        return [{"hostname": host, "host": adresse, "port": port, "family": socket.AF_INET, "proto": 0,
                 "flags": socket.AI_NUMERICHOST}]

    async def close(self) -> None:
        return None


@pytest.fixture
async def quelle(aiohttp_server):
    aufrufe: list[str] = []

    async def klein(request: web.Request) -> web.Response:
        aufrufe.append(request.path)
        return web.Response(body=b"<tv/>")

    async def weiter(request: web.Request) -> web.Response:
        aufrufe.append(request.path)
        raise web.HTTPFound(request.query["ziel"])

    async def kreis(request: web.Request) -> web.Response:
        aufrufe.append(request.path)
        raise web.HTTPFound("/kreis")

    app = web.Application()
    app.router.add_get("/klein", klein)
    app.router.add_get("/weiter", weiter)
    app.router.add_get("/kreis", kreis)
    server = await aiohttp_server(app)
    server.aufrufe = aufrufe
    return server


@pytest.fixture
def oeffentlich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Testquelle auf 127.0.0.1 gilt als öffentlicher Server; Namen werden nicht wirklich aufgelöst."""
    echt = epg._nicht_oeffentlich

    def nicht_oeffentlich(ip) -> bool:
        return False if str(ip) == "127.0.0.1" else echt(ip)

    async def adressen(rechner: str, port: int):
        return [ipaddress.ip_address(rechner)]

    monkeypatch.setattr(epg, "_nicht_oeffentlich", nicht_oeffentlich)
    monkeypatch.setattr(epg, "_adressen", adressen)


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
async def test_weiterleitung_innerhalb_der_quelle(hass: HomeAssistant, quelle, tmp_path: Path) -> None:
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    ziel = str(tmp_path / "epg.xml")
    assert await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/weiter?ziel=/klein", ziel) == 5
    assert quelle.aufrufe == ["/weiter", "/klein"]


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
@pytest.mark.parametrize(
    "ziel",
    [
        "http://192.168.178.1/relay/0?turn=on",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/",
        "http://localhost:8123/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:192.168.1.1]/",
        "http://224.0.0.251/",
        "http://0.0.0.0/",
    ],
)
async def test_weiterleitung_ins_lokale_netz_wird_abgelehnt(
    hass: HomeAssistant, quelle, tmp_path: Path, ziel: str
) -> None:
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    datei = tmp_path / "epg.xml"
    with pytest.raises(epg.EpgFehler, match="lokalen Netz") as fehler:
        await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/weiter?ziel={ziel}", str(datei))
    assert quelle.aufrufe == ["/weiter"]
    assert sitzung.rechner == ["127.0.0.1"]
    assert URL(ziel).host not in str(fehler.value)  # nie die Adresse im Fehlertext
    assert not datei.exists() and not Path(str(datei) + ".teil").exists()


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
async def test_zu_viele_weiterleitungen(hass: HomeAssistant, quelle, tmp_path: Path) -> None:
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    with pytest.raises(epg.EpgFehler, match="Weiterleitungen"):
        await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/kreis", str(tmp_path / "epg.xml"))
    assert len(quelle.aufrufe) == epg.MAX_WEITERLEITUNGEN + 1


@pytest.mark.usefixtures("socket_enabled")
async def test_quelle_im_heimnetz_darf_im_heimnetz_weiterleiten(hass: HomeAssistant, quelle, tmp_path: Path) -> None:
    """Eigener Tvheadend: 127.0.0.1 ist lokal, eine Weiterleitung ins Heimnetz bleibt erlaubt."""
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    ziel = str(tmp_path / "epg.xml")
    weiter = f"http://127.0.0.1:{quelle.port}/klein"
    assert await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/weiter?ziel={weiter}", ziel) == 5
    assert epg.quelle_lokal(URL("http://192.168.1.5:9981/xmltv")) is True
    assert await epg._weiterleitung_abgelehnt(URL("http://192.168.1.5:9981/a"), URL("http://192.168.1.6/b"), True) is None


@pytest.mark.parametrize(
    ("url", "lokal"),
    [
        ("http://192.168.1.5:9981/xmltv", True), ("http://[fd00::5]/x", True), ("http://localhost:9981/", True),
        ("http://tvh.lan/x", True), ("http://tvh.local/x", True), ("http://tvh.home.arpa/x", True),
        ("http://TVH.LAN./x", True),
        ("http://tvh.example/x", False), ("http://93.184.216.34/x", False), ("http://tvheadend:9981/", False),
        ("http://lan/x", False), (DEFAULT_URL, False), (DEFAULT_URL_ERSATZ, False),
    ],
)
def test_quelle_lokal_nur_nach_der_adresse(url: str, lokal: bool) -> None:
    assert epg.quelle_lokal(URL(url)) is lokal


@pytest.mark.usefixtures("socket_enabled")
async def test_privates_dns_der_quelle_schaltet_die_pruefung_nicht_ab(
    hass: HomeAssistant, quelle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wer das DNS der Quelle beherrscht, antwortet bei der Prüfung privat und beim Verbinden öffentlich."""

    async def adressen(rechner: str, port: int):
        if rechner == "quelle.test":
            return [ipaddress.ip_address("192.168.1.5")]
        return [ipaddress.ip_address(rechner)]

    monkeypatch.setattr(epg, "_adressen", adressen)
    sitzung = Bewacht(async_get_clientsession(hass), {"quelle.test"}, umleiten=frozenset({"quelle.test"}))
    ziel = "http://192.168.178.1/relay/0?turn=on"
    with pytest.raises(epg.EpgFehler, match="lokalen Netz"):
        await epg.herunterladen(sitzung, f"http://quelle.test:{quelle.port}/weiter?ziel={ziel}", str(tmp_path / "x"))
    assert sitzung.rechner == ["quelle.test"]


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
async def test_rebinding_beim_verbinden_wird_abgelehnt(
    hass: HomeAssistant, quelle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Das Ziel der Weiterleitung ist bei der Prüfung öffentlich und beim Verbinden Loopback."""
    dns = FalschesDns({"ziel.test": ["127.0.0.2"]})
    monkeypatch.setattr(epg, "_innerer_aufloeser", lambda: dns)
    echt_adressen = epg._adressen

    async def adressen(rechner: str, port: int):
        return [OEFFENTLICH] if rechner == "ziel.test" else await echt_adressen(rechner, port)

    monkeypatch.setattr(epg, "_adressen", adressen)
    url = f"http://127.0.0.1:{quelle.port}/weiter?ziel=http://ziel.test:{quelle.port}/klein"
    async with epg.eigene_sitzung(url) as eigene:
        with pytest.raises(epg.EpgFehler, match="DNS"):
            await epg.herunterladen(Bewacht(eigene, {"127.0.0.1", "ziel.test"}), url, str(tmp_path / "x"))
    assert dns.fragen == ["ziel.test"]
    assert quelle.aufrufe == ["/weiter"]


async def test_resolver_laesst_die_eingetragene_quelle_einmal_privat_zu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Split-DNS: Die Quelle selbst darf privat aufgelöst werden, aber nur bei der ersten Auflösung."""
    dns = FalschesDns({"epg.example.org": ["192.168.1.5"], "anders.example.org": ["10.0.0.1"]})
    monkeypatch.setattr(epg, "_innerer_aufloeser", lambda: dns)
    aufloeser = epg._Aufloeser(False, "EPG.example.org.")
    assert (await aufloeser.resolve("epg.example.org", 443))[0]["host"] == "192.168.1.5"
    for rechner in ("epg.example.org", "anders.example.org", "localhost"):
        dns._antworten.setdefault(rechner, ["93.184.216.34"])
        with pytest.raises(OSError, match="lokalen Netz"):
            await aufloeser.resolve(rechner, 443)
    lokal = epg._Aufloeser(True, None)
    assert (await lokal.resolve("anders.example.org", 80))[0]["host"] == "10.0.0.1"


async def test_kein_wechsel_von_https_auf_http() -> None:
    grund = await epg._weiterleitung_abgelehnt(URL("https://quelle.example/a"), URL("http://quelle.example/b"), True)
    assert grund == "von https auf http"
    assert await epg._weiterleitung_abgelehnt(URL("http://quelle.example/a"), URL("ftp://quelle.example/b"), True)


@pytest.mark.parametrize(
    ("adresse", "lokal"),
    [
        ("127.0.0.1", True), ("10.1.2.3", True), ("172.16.0.1", True), ("192.168.0.1", True),
        ("169.254.169.254", True), ("100.64.0.1", True), ("224.0.0.1", True), ("0.0.0.0", True),
        ("::1", True), ("fd00::1", True), ("fe80::1", True), ("::ffff:10.0.0.1", True), ("ff02::1", True),
        ("93.184.216.34", False), ("2a00:1450:4001::1", False),
    ],
)
def test_nicht_oeffentlich(adresse: str, lokal: bool) -> None:
    assert epg._nicht_oeffentlich(ipaddress.ip_address(adresse)) is lokal


async def test_unaufloesbar(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scheitert(_rechner: str, _port: int):
        raise OSError("keine Auflösung")

    monkeypatch.setattr(epg, "_adressen", scheitert)
    assert await epg._lokal("quelle.example", 80) is True  # Ziel: im Zweifel ablehnen
    assert epg.quelle_lokal(URL("http://quelle.example/")) is False  # Start: ohne DNS, im Zweifel streng


async def test_ungueltige_adresse_ist_epgfehler(hass: HomeAssistant, tmp_path: Path) -> None:
    with pytest.raises(epg.EpgFehler):
        await epg.herunterladen(async_get_clientsession(hass), "ftp://quelle.example/epg.xml", str(tmp_path / "x"))
