from .base import Adapter, PrinterError, Status  # noqa: F401


def make_adapter(cfg: dict) -> Adapter:
    kind = cfg["kind"]
    if kind == "prusalink":
        from .prusalink import PrusaLinkAdapter
        return PrusaLinkAdapter(cfg)
    if kind == "bambu":
        from .bambu import BambuAdapter
        return BambuAdapter(cfg)
    if kind == "mock":
        from .mock import MockAdapter
        return MockAdapter(cfg)
    raise ValueError(f"unknown printer kind {kind!r}")


FILE_KINDS = {
    "prusalink": {"bgcode", "gcode"},
    "bambu": {"3mf"},
    "mock": {"bgcode", "gcode", "3mf"},
}
