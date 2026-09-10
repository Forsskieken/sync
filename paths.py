#!/usr/bin/env python3
"""
paths.py - de padgrendel, deelbaar tussen de webweg en de CLI.

Buiten ALLOWED_ROOTS wordt niets gelezen of geschreven. Die controle stond in
app.py als safe_path(); hier staat hij zonder HTTP-laag, zodat convert.py hem
kan gebruiken zonder FastAPI te importeren. app.py houdt zijn eigen safe_path
als omhulling die de HTTP-fout opgooit.
"""

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("paths")

DEFAULT_ALLOWED_ROOTS = '["/mnt"]'


def allowed_roots() -> List[Path]:
    """Leest ALLOWED_ROOTS (JSON-lijst met paden) uit de omgeving.

    Bij onzin in de omgeving stoppen we meteen met een leesbare melding in
    plaats van later met een JSONDecodeError of een lege lijst: een lege lijst
    zou stilzwijgend *alles* weigeren en dat lijkt op een rechtenprobleem.
    """
    raw = os.environ.get("ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ALLOWED_ROOTS is geen geldige JSON-lijst: {e}") from e
    if not isinstance(value, list) or not value:
        raise ValueError("ALLOWED_ROOTS moet een niet-lege JSON-lijst met paden zijn")
    roots = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"ALLOWED_ROOTS bevat een ongeldig pad: {item!r}")
        roots.append(Path(item))
    return roots


def resolve_in_roots(p: str, roots: List[Path]) -> Optional[Path]:
    """Absoluut pad binnen een van de roots, of None.

    resolve() lost eerst symlinks en '..' op; pas daarna wordt de uitkomst met
    de (eveneens opgeloste) root vergeleken. Andersom zou een symlink in de
    share zo de grendel uit lopen.

    Een pad dat niet eens op te lossen is telt als "ligt er buiten", niet als
    een fout die naar boven mag. resolve() gooit namelijk ValueError bij een
    NUL-teken in het pad ("?path=%00") en OSError bij een te lang pad of een
    lus van symlinks: langs de webweg werd dat een 500 met traceback in plaats
    van een 403, en langs convert.py zou het een bulkrun onderweg afbreken op
    één raar bestand. De WARN-regel houdt het zichtbaar - stil weigeren zou
    lijken op een rechtenprobleem.
    """
    if not isinstance(p, str) or not p.strip():
        # Path("").resolve() geeft de werkmap terug, en die kan toevallig binnen
        # een root liggen. Een leeg pad is geen pad.
        log.warning("pad geweigerd: leeg of geen tekst (%r)", type(p).__name__)
        return None
    try:
        q = Path(p).resolve()
    except (ValueError, OSError) as e:
        # %r: een NUL-teken of een regelovergang uit de invoer hoort niet
        # onvertaald in een journaalregel terecht te komen.
        log.warning("pad geweigerd, niet op te lossen: %s (%.200r)", e, p)
        return None
    for root in roots:
        try:
            q.relative_to(root.resolve())
            return q
        except ValueError:
            continue
        except OSError as e:
            # Een root die zelf niet op te lossen is (mount weg, rechten kwijt)
            # mag de andere roots niet meeslepen.
            log.warning("toegelaten map %s is niet op te lossen: %s", root, e)
            continue
    return None
