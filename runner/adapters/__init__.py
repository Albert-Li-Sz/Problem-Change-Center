from .protocol import Adapter, Detection, READABLE_FORMATS, WRITABLE_FORMATS

__all__ = [
    "ADAPTERS",
    "Adapter",
    "Detection",
    "READABLE_FORMATS",
    "WRITABLE_FORMATS",
]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    if name == "ADAPTERS":
        from .registry import ADAPTERS

        return ADAPTERS
    raise AttributeError(name)
