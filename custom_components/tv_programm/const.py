"""Konstanten für TV-Programm."""

DOMAIN = "tv_programm"

CONF_URL = "url"  # Hauptquelle
CONF_URL_ERSATZ = "url_ersatz"  # nur für Sender, die in der Hauptquelle fehlen
CONF_UHRZEIT = "uhrzeit"
CONF_SENDER = "sender"

# Hauptquelle: Zeiten korrekt, gepackt ~4 MB, aber ohne dritte Programme (NDR …)
DEFAULT_URL = "https://epgshare01.online/epgshare01/epg_ripper_DE1.xml.gz"
# Ersatzquelle: hat auch die dritten Programme, ~45 MB; nach der Zeitumstellung 25.10.2026 bei ZDF, SAT.1, VOX,
# RTLZWEI und ARTE eine Stunde zu früh – deshalb nur noch für Sender, die der Hauptquelle fehlen
DEFAULT_URL_ERSATZ = "https://iptv-epg.org/files/epg-de.xml"
ALTE_URL = DEFAULT_URL_ERSATZ  # Vorgabe bis v0.2.0
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

ABENDZEIT = (20, 15)

DIENST_ABFRAGEN = "abfragen"
MAX_TREFFER = 15
