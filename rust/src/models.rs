//! Value mirrors of `src/cutting/models.py`.
//!
//! Every domain struct in the Python package is effectively immutable — the
//! whole package contains exactly two attribute assignments, both the cached
//! `area` in `__post_init__` — so these are all `Copy` value types.

/// Mirror of `models.Rectangle`. `area` is cached at construction, as it is in
/// Python, because the packer's inner loop reads it once per (piece, gap) pair.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    pub area: f64,
}

impl Rect {
    pub fn new(x: f64, y: f64, width: f64, height: f64) -> Self {
        Rect {
            x,
            y,
            width,
            height,
            area: width * height,
        }
    }
}

/// Mirror of `models.Cut`. `length` is the saw's travel along the cut axis;
/// kerf is perpendicular and does not affect it.
#[derive(Debug, Clone, Copy)]
pub struct Cut {
    pub x: f64,
    pub y: f64,
    pub length: f64,
    pub is_horizontal: bool,
}

/// Mirror of `models.Piece`, with the string `id` replaced by two integers.
///
/// `index` is the position in the caller's pool (how a placement is attributed
/// back to a concrete Python `Piece`). `id_rank` is the rank of that piece's
/// `id` in the pool's lexicographic id order, and it exists because every
/// comparator in `constructors.SORT_KEYS` ends in `p.id` as its final
/// tiebreak: comparing ranks reproduces Python's string ordering exactly
/// without moving strings across the FFI boundary.
#[derive(Debug, Clone, Copy)]
pub struct Piece {
    pub index: u32,
    pub id_rank: u32,
    pub width: f64,
    pub height: f64,
    pub area: f64,
    pub can_rotate: bool,
    pub priority: i32,
}

impl Piece {
    pub fn new(
        index: u32,
        id_rank: u32,
        width: f64,
        height: f64,
        can_rotate: bool,
        priority: i32,
    ) -> Self {
        Piece {
            index,
            id_rank,
            width,
            height,
            area: width * height,
            can_rotate,
            priority,
        }
    }
}

/// Mirror of `models.PlacedPiece`; carries the pool index instead of the object.
#[derive(Debug, Clone, Copy)]
pub struct PlacedPiece {
    pub index: u32,
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    pub rotated: bool,
}

/// Mirror of `parameters.CuttingParameters`.
#[derive(Debug, Clone, Copy)]
pub struct CuttingParams {
    pub kerf: f64,
    pub top_trim: f64,
    pub bottom_trim: f64,
    pub left_trim: f64,
    pub right_trim: f64,
}

/// Mirror of `enums.SplitRule`. Discriminants are assigned by the Python
/// bridge, which owns the mapping — see `_SPLIT_RULE_CODES` in the bridge.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SplitRule {
    ShorterLeftoverAxis = 0,
    LongerLeftoverAxis = 1,
    MinimizeArea = 2,
    MaximizeArea = 3,
    ShorterAxis = 4,
    LongerAxis = 5,
}

impl SplitRule {
    pub fn from_code(code: u8) -> Option<Self> {
        match code {
            0 => Some(SplitRule::ShorterLeftoverAxis),
            1 => Some(SplitRule::LongerLeftoverAxis),
            2 => Some(SplitRule::MinimizeArea),
            3 => Some(SplitRule::MaximizeArea),
            4 => Some(SplitRule::ShorterAxis),
            5 => Some(SplitRule::LongerAxis),
            _ => None,
        }
    }
}

/// Mirror of `enums.PackingStrategy`, used here as the free-rect *selection*
/// axis: `MaxEfficiency` is Best-Area-Fit, `LongOffcuts` is Bottom-Left.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Selection {
    MaxEfficiency = 0,
    LongOffcuts = 1,
}

impl Selection {
    pub fn from_code(code: u8) -> Option<Self> {
        match code {
            0 => Some(Selection::MaxEfficiency),
            1 => Some(Selection::LongOffcuts),
            _ => None,
        }
    }
}

/// Which orientation `create_split_rectangles` chose; `cuts_for` derives the
/// cut lengths from it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Orientation {
    VerticalFirst,
    HorizontalFirst,
}
