from abc import ABC, abstractmethod

class OptionsFeed(ABC):
    name = "base"
    provenance = "EST"

    @abstractmethod
    def available(self) -> bool:
        """Can this feed run right now? (key present, lib installed, etc.)"""

    @abstractmethod
    def fetch_chain(self, symbol: str):
        """Return a ChainSnapshot. Raise on failure; orchestrator catches."""