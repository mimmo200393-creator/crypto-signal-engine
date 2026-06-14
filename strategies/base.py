"""
strategies/base.py
BaseStrategy astratta e Signal dataclass - Framework V2.1
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


@dataclass
class Signal:
    """
    Oggetto standard prodotto da ogni strategia nel framework V2.1.

    raw_score    = punteggio tecnico calcolato dalla strategia (0-10 tipicamente)
    final_score  = raw_score + regime_bonus - correlation_penalty (nessun capping)
    trade_status = GENERATED -> APPROVED/REJECTED -> NOTIFIED -> OPEN -> TP/SL
    """
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    strategy_name: str = ""
    strategy_version: str = ""
    asset: str = ""
    direction: str = ""
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    rr: float = 0.0
    raw_score: float = 0.0
    final_score: float = 0.0
    market_regime: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    additional_context: dict = field(default_factory=dict)

    trade_status: str = "GENERATED"
    rejection_reason: Optional[str] = None


class BaseStrategy(ABC):
    """
    Classe base astratta per tutte le strategie V2.1.

    Ogni strategia concreta implementa:
        name        (str)
        version     (str)
        generate_signal(market_data) -> Optional[Signal]

    market_data standard:
        {
            "asset":     str,
            "direction": str,
            "df_h1":     pd.DataFrame,
            "df_h4":     pd.DataFrame,
            "config":    dict,
        }
    """
    name: str = ""
    version: str = ""

    @abstractmethod
    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        raise NotImplementedError

    def __repr__(self):
        return f"<Strategy {self.name} {self.version}>"
