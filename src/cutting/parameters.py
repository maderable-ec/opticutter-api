from dataclasses import dataclass


@dataclass
class CuttingParameters:
    """Cutting parameters for the optimizer"""

    kerf: float = 0.0
    top_trim: float = 0.0
    bottom_trim: float = 0.0
    left_trim: float = 0.0
    right_trim: float = 0.0

    def __post_init__(self):
        if self.kerf < 0:
            raise ValueError(f"Kerf cannot be negative: {self.kerf}")
        if self.top_trim < 0:
            raise ValueError(f"Top trim cannot be negative: {self.top_trim}")
        if self.bottom_trim < 0:
            raise ValueError(f"Bottom trim cannot be negative: {self.bottom_trim}")
        if self.left_trim < 0:
            raise ValueError(f"Left trim cannot be negative: {self.left_trim}")
        if self.right_trim < 0:
            raise ValueError(f"Right trim cannot be negative: {self.right_trim}")

        # Coerced, because these are where integers enter the geometry: the
        # trims seed the packer's first free rectangle, so ``CuttingParameters(
        # kerf=5, top_trim=10)`` used to emit ``"x": 10`` where the same run
        # with float parameters emitted ``"x": 10.0``. Values never differ —
        # renderings do, and layouts are serialized into order snapshots and
        # cached by input hash. The Rust kernel is f64 throughout
        # (``rust_backend``), so without this the two backends would produce
        # byte-different JSON for numerically identical geometry.
        self.kerf = float(self.kerf)
        self.top_trim = float(self.top_trim)
        self.bottom_trim = float(self.bottom_trim)
        self.left_trim = float(self.left_trim)
        self.right_trim = float(self.right_trim)
