"""
macro.py
MacroEventProvider astratto + implementazione YAML.

La V1.0 NON usa gli eventi macro come filtro operativo: vengono solo
registrati nel trade per finalita' statistiche/di consapevolezza.
"""

import os
import yaml
import logging
from datetime import datetime, timezone
from abc import ABC, abstractmethod

logger = logging.getLogger("macro")


class MacroEventProvider(ABC):
    @abstractmethod
    def get_active_event(self, current_dt: datetime, window_minutes: int):
        """
        Ritorna un dict {"type": str, "minutes_to_release": int} se esiste
        un evento macro entro `window_minutes` (prima o dopo current_dt),
        altrimenti None.
        """
        raise NotImplementedError


class YAMLMacroProvider(MacroEventProvider):
    """
    Legge eventi da un file YAML con formato:

        events:
          - type: "FOMC"
            datetime: "2026-07-30T18:00:00Z"
          - type: "CPI_USA"
            datetime: "2026-07-11T12:30:00Z"

    Il file viene ricaricato ad ogni chiamata (e' un file piccolo,
    aggiornato manualmente -> nessun problema di performance).
    """

    def __init__(self, filepath: str):
        self.filepath = filepath

    def _load_events(self):
        if not os.path.exists(self.filepath):
            logger.warning("File eventi macro non trovato: %s", self.filepath)
            return []

        with open(self.filepath, "r") as f:
            data = yaml.safe_load(f) or {}

        events = data.get("events", [])
        parsed = []
        for ev in events:
            try:
                dt = datetime.fromisoformat(ev["datetime"].replace("Z", "+00:00"))
                parsed.append({"type": ev["type"], "datetime": dt})
            except (KeyError, ValueError) as e:
                logger.warning("Evento macro malformato ignorato: %s (%s)", ev, e)
        return parsed

    def get_active_event(self, current_dt: datetime, window_minutes: int):
        events = self._load_events()
        if not events:
            return None

        if current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=timezone.utc)

        best = None
        best_abs_minutes = None

        for ev in events:
            delta = ev["datetime"] - current_dt
            minutes_to = delta.total_seconds() / 60.0

            if abs(minutes_to) <= window_minutes:
                if best is None or abs(minutes_to) < best_abs_minutes:
                    best = ev
                    best_abs_minutes = abs(minutes_to)

        if best is None:
            return None

        return {
            "type": best["type"],
            "minutes_to_release": int(round((best["datetime"] - current_dt).total_seconds() / 60.0)),
        }


def get_provider(config: dict) -> MacroEventProvider:
    """
    Factory: ritorna l'istanza del provider configurato.
    V1.0 usa sempre YAMLMacroProvider, ma il Signal Engine dipende
    solo dall'interfaccia astratta MacroEventProvider.
    """
    return YAMLMacroProvider(config.get("MACRO_EVENTS_FILE", "macro_events.yaml"))
