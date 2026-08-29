"""
Localised, web-appropriate wording for the notes the merge engine raises.

The engine speaks English and describes the desktop tool (it mentions the
listing-map.json file it writes). On the web the same situations need Italian
first, and the guest-code mapping lives in the browser, not in a file. Rather
than fork the engine, its notes are matched here and re-worded.

Anything unmatched is passed through unchanged in both languages, so a new
engine note is never swallowed - it just appears in English until someone adds
a pattern for it.
"""

import re

# (compiled pattern, severity, italian template, english template)
# Templates use {0}, {1}... for the regex groups.
RULES = [
    (r"THE DATES ARE NOT CONTINUOUS - (\d+) day\(s\) missing \(([^)]*)\)",
     "critical",
     "Le date non sono continue: mancano {0} giorni ({1}). SPOT interrompe "
     "l'elaborazione al primo giorno mancante, quindi tutto il resto del mese "
     "verrebbe ignorato. Riesporta il mese completo.",
     "The dates are not continuous: {0} day(s) missing ({1}). SPOT stops "
     "processing at the first gap, so everything after it would be ignored. "
     "Re-export the month covering every day."),

    (r"(\d+) guest code\(s\) were used by more than one listing.*?\(([^)]*)\)",
     "info",
     "{0} codice/i cliente erano usati da più di un annuncio: ogni codice è "
     "stato reso univoco per annuncio ({1}). È necessario, altrimenti SPOT "
     "considererebbe due ospiti come la stessa persona. La corrispondenza "
     "resta salvata in questo browser e viene riusata ogni mese.",
     "{0} guest code(s) were used by more than one listing, so every code was "
     "made unique per listing ({1}). This is required: SPOT identifies guests "
     "by this code and would otherwise treat two people as one. The mapping is "
     "kept in this browser and reused every month."),

    (r"(.+?) has no entry for (\d+) date\(s\) \(([^)]*)\)",
     "warn",
     "{0} non contiene {1} giorni ({2}): per quelle date sono stati conteggiati "
     "solo gli altri file. Di solito significa che quell'esportazione copre un "
     "periodo più corto.",
     "{0} has no entry for {1} date(s) ({2}); only the other files were counted "
     "for those days. Usually it means that export covers a shorter period."),

    (r"Input covers more than one month: (.+)",
     "warn",
     "I file caricati coprono più di un mese ({0}). Carica un mese alla volta.",
     "The uploaded files cover more than one month ({0}). Upload one month at "
     "a time."),

    (r"(.+?) already contains two blocks for (\S+)",
     "warn",
     "{0} contiene due blocchi per il {1}: sono stati sommati.",
     "{0} contains two blocks for {1}; they were added together."),

    (r"(\S+): rooms occupied \((\d+)\) exceeds rooms available \((\d+)\)",
     "critical",
     "{0}: camere occupate ({1}) maggiori delle camere disponibili ({2}). "
     "SPOT rifiuta questo dato: controlla l'esportazione.",
     "{0}: rooms occupied ({1}) exceeds rooms available ({2}). SPOT rejects "
     "this - check the export."),

    (r"(\S+): rooms available \((\d+)\) exceeds bed places \((\d+)\)",
     "critical",
     "{0}: camere disponibili ({1}) maggiori dei posti letto ({2}). SPOT "
     "rifiuta questo dato.",
     "{0}: rooms available ({1}) exceeds bed places ({2}). SPOT rejects this."),

    (r"(\S+): type is MP but there are no arrivals or departures",
     "critical",
     "{0}: il giorno è marcato MP (movimento presente) ma non contiene né "
     "arrivi né partenze.",
     "{0}: the day is marked MP (movement present) but carries no arrivals or "
     "departures."),

    (r"(\S+): type is (\S+), so <arrivi>/<partenze> must not be present",
     "critical",
     "{0}: il tipo è {1}, quindi non devono esserci arrivi o partenze.",
     "{0}: the type is {1}, so arrivals and departures must not be present."),

    (r"(\S+): unrecognised movement type\(s\) (.+?) - ",
     "critical",
     "{0}: tipo di movimento non valido {1}. Lo schema ammette solo MP, NM, EC.",
     "{0}: invalid movement type {1}. The schema allows only MP, NM, EC."),

    (r"(\S+): the premises are marked closed \(EC\)",
     "warn",
     "{0}: la struttura è indicata come chiusa (EC) ma i contatori non sono a "
     "zero.",
     "{0}: the premises are marked closed (EC) but the counters are not zero."),

    (r"the merged file does not satisfy the official XSD",
     "critical",
     "Il file unito non rispetta lo schema ufficiale: il portale lo "
     "rifiuterebbe.",
     "The merged file does not satisfy the official schema; the portal would "
     "reject it."),

    (r"(\S+): date '?([^']+)'? is not zero-padded",
     "info",
     "{0}: la data {1} non usa due cifre per mese e giorno.",
     "{0}: the date {1} is not zero-padded."),

    (r"root/@(\S+) differs between files",
     "warn",
     "L'attributo {0} della radice è diverso tra i file: è stato tenuto il "
     "primo valore.",
     "The root attribute {0} differs between files; the first value was kept."),
]

COMPILED = [(re.compile(p, re.S), sev, it, en) for p, sev, it, en in RULES]

CRITICAL_HINTS = ("NOT CONTINUOUS", "does not satisfy", "exceeds", "invalid")


def localise(note):
    """Turn one engine note into {severity, it, en, raw}."""
    for pattern, severity, it_tpl, en_tpl in COMPILED:
        match = pattern.search(note)
        if match:
            groups = match.groups()
            try:
                return {"severity": severity,
                        "it": it_tpl.format(*groups),
                        "en": en_tpl.format(*groups),
                        "raw": note}
            except (IndexError, KeyError):
                break
    severity = "critical" if any(h in note for h in CRITICAL_HINTS) else "info"
    return {"severity": severity, "it": note, "en": note, "raw": note}


def localise_all(notes):
    return [localise(n) for n in notes]
