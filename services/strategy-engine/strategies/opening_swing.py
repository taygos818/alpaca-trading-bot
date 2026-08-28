from dataclasses import replace

from .tier2_swing import SwingStrategy
from .tier4_opening import OpeningOpportunityStrategy


class _ObservationBuffer:
    def __init__(self):
        self.observations = []


class OpeningThenSwingStrategy:
    """Run opening confirmation and swing discovery through one execution loop.

    Keeping both strategies in one process avoids races between independent
    engines while preserving the execution loop's provenance and risk gates.
    """

    name = "tier2_swing_with_opening"

    def __init__(self):
        self.opening = OpeningOpportunityStrategy()
        self.swing = SwingStrategy()
        self.data = _ObservationBuffer()

    def _capture(self, provider):
        self.data.observations[:] = list(getattr(provider, "observations", []))[-50:]

    def run_cycle(self, broker_state=None):
        opening_observations = getattr(self.opening.data, "observations", [])
        opening_observations.clear()
        opening_intent = self.opening.run_cycle(broker_state)
        if opening_intent.action != "hold":
            self._capture(self.opening.data)
            return opening_intent

        owned_symbols = self.opening.owned_position_symbols(broker_state)
        swing_state = broker_state
        if broker_state is not None and owned_symbols:
            swing_state = replace(
                broker_state,
                positions={
                    symbol: position
                    for symbol, position in broker_state.positions.items()
                    if symbol not in owned_symbols
                },
            )
        swing_observations = getattr(self.swing.data, "observations", [])
        swing_observations.clear()
        swing_intent = self.swing.run_cycle(swing_state)
        self._capture(self.swing.data)
        return swing_intent
