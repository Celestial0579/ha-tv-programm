"""Konstanten für TV-Programm."""

DOMAIN = "tv_programm"

CONF_URL = "url"
CONF_UHRZEIT = "uhrzeit"
CONF_SENDER = "sender"

DEFAULT_URL = "https://iptv-epg.org/files/epg-de.xml"
DEFAULT_UHRZEIT = "04:00:00"

# Vorgabe-Auswahl (Anzeigenamen ohne Länderpräfix); nur vorhandene werden übernommen
STANDARD_SENDER = [
    "Das Erste", "ZDF", "RTL", "SAT.1", "ProSieben", "VOX", "Kabel Eins", "RTLZWEI", "Super RTL",
    "NDR Fernsehen Niedersachsen", "3sat", "arte", "ZDFneo", "ONE", "tagesschau24", "phoenix", "KiKA",
    "DMAX", "sixx", "NITRO", "ntv", "WELT", "Comedy Central", "ProSieben MAXX", "Tele 5",
]

# Umgangssprachliche Namen -> Sender (beides ohne Sonderzeichen, klein)
SPITZNAMEN = {
    "ard": "daserste", "erste": "daserste", "ersten": "daserste", "daserste": "daserste",
    "zweite": "zdf", "zweiten": "zdf", "daszweite": "zdf",
    "dritte": "ndrfernsehenniedersachsen", "dritten": "ndrfernsehenniedersachsen", "ndr": "ndrfernsehenniedersachsen",
    "pro7": "prosieben", "pro7maxx": "prosiebenmaxx", "sat1": "sat1", "rtl2": "rtlzwei", "rtl2": "rtlzwei",
    "kabel1": "kabeleins", "zdfneo": "zdfneo", "ntv": "ntv", "welt": "welt", "tele5": "tele5",
}

DIENST_ABFRAGEN = "abfragen"
MAX_TREFFER = 15
