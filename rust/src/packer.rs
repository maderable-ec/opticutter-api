//! Port of `src/cutting/packer.py` — the single-bin guillotine packer.
//!
//! This is a *transliteration*, not a reimplementation: the Python version is
//! the oracle and its geometry is cached by input hash, so every tie-break,
//! comparison order and float expression here mirrors the original statement
//! for statement. Where the Python looks redundant it is kept anyway; the place
//! to make it faster is the language, not the algorithm.

use crate::models::{Cut, CuttingParams, Orientation, PlacedPiece, Piece, Rect, Selection, SplitRule};

/// Fit score of a gap for a piece, lower is better.
///
/// Mirrors the tuple `_place_piece` builds. `MaxEfficiency` compares
/// `(leftover, secondary)`; `LongOffcuts` compares `(x, y, leftover,
/// secondary)`. Both are modelled as four lexicographic slots, with the
/// unused leading pair held at 0.0 for `MaxEfficiency` — they compare equal on
/// every candidate, so they never affect the ordering.
#[derive(Debug, Clone, Copy)]
struct Score([f64; 4]);

impl Score {
    #[inline]
    fn better_than(&self, other: &Score) -> bool {
        for i in 0..4 {
            let a = self.0[i];
            let b = other.0[i];
            if a < b {
                return true;
            }
            if a > b {
                return false;
            }
        }
        false
    }
}

pub struct Packer {
    kerf: f64,
    // The trims are consumed building the initial free rect and never read
    // again, so they are not kept as fields.
    min_rect_size: f64,
    split_rule: SplitRule,
    selection: Selection,
    pub remainders: Vec<Rect>,
    pub placed: Vec<PlacedPiece>,
    pub cuts: Vec<Cut>,
}

impl Packer {
    /// `None` when the trims exceed the sheet — the `ValueError` that
    /// `constructors.greedy_fill` catches and turns into "this bin is unusable".
    pub fn new(
        width: f64,
        height: f64,
        params: &CuttingParams,
        split_rule: SplitRule,
        selection: Selection,
        min_rect_size: f64,
    ) -> Option<Self> {
        let kerf = params.kerf.max(0.0);
        let top_trim = params.top_trim.max(0.0);
        let bottom_trim = params.bottom_trim.max(0.0);
        let left_trim = params.left_trim.max(0.0);
        let right_trim = params.right_trim.max(0.0);
        let min_rect_size = min_rect_size.max(0.01);

        if left_trim + right_trim >= width {
            return None;
        }
        if top_trim + bottom_trim >= height {
            return None;
        }

        let usable_width = width - left_trim - right_trim;
        let usable_height = height - top_trim - bottom_trim;

        Some(Packer {
            kerf,
            min_rect_size,
            split_rule,
            selection,
            remainders: vec![Rect::new(left_trim, bottom_trim, usable_width, usable_height)],
            placed: Vec::new(),
            cuts: Vec::new(),
        })
    }

    /// Places `pieces` in the order given (the callers all pre-sort).
    pub fn optimize(&mut self, pieces: &[Piece]) {
        for piece in pieces {
            self.place_piece(piece);
        }
    }

    fn place_piece(&mut self, piece: &Piece) -> bool {
        let mut best_index: isize = -1;
        let mut best_rotated = false;
        let mut best_score: Option<Score> = None;

        let piece_w = piece.width;
        let piece_h = piece.height;
        let piece_area = piece.area;
        let can_rotate = piece.can_rotate;
        let long_offcuts = self.selection == Selection::LongOffcuts;

        for (i, rect) in self.remainders.iter().enumerate() {
            let rect_w = rect.width;
            let rect_h = rect.height;
            let leftover = rect.area - piece_area;

            // Unrotated first, then rotated: the rotated branch is evaluated in
            // the same iteration and with a strict `<`, so an exact tie keeps
            // the unrotated orientation — matching the Python.
            if rect_w >= piece_w && rect_h >= piece_h {
                let score = if long_offcuts {
                    Score([rect.x, rect.y, leftover, rect_w - piece_w])
                } else {
                    Score([leftover, rect_w - piece_w, 0.0, 0.0])
                };
                if best_score.is_none() || score.better_than(best_score.as_ref().unwrap()) {
                    best_score = Some(score);
                    best_index = i as isize;
                    best_rotated = false;
                }
            }

            if can_rotate && rect_w >= piece_h && rect_h >= piece_w {
                let score = if long_offcuts {
                    Score([rect.x, rect.y, leftover, rect_w - piece_h])
                } else {
                    Score([leftover, rect_w - piece_h, 0.0, 0.0])
                };
                if best_score.is_none() || score.better_than(best_score.as_ref().unwrap()) {
                    best_score = Some(score);
                    best_index = i as isize;
                    best_rotated = true;
                }
            }
        }

        if best_index < 0 {
            return false;
        }
        let best_index = best_index as usize;
        let rect = self.remainders[best_index];

        let (placed_width, placed_height) = if best_rotated {
            (piece.height, piece.width)
        } else {
            (piece.width, piece.height)
        };

        let placed = PlacedPiece {
            index: piece.index,
            x: rect.x,
            y: rect.y,
            width: placed_width,
            height: placed_height,
            rotated: best_rotated,
        };
        self.placed.push(placed);
        self.split_remainder(best_index, &placed);
        true
    }

    fn split_remainder(&mut self, rect_index: usize, placed: &PlacedPiece) {
        let rect = self.remainders.remove(rect_index);

        let width_leftover = rect.width - placed.width;
        let height_leftover = rect.height - placed.height;

        let effective_width = placed.width + if width_leftover > 0.0 { self.kerf } else { 0.0 };
        let effective_height = placed.height + if height_leftover > 0.0 { self.kerf } else { 0.0 };

        let (new_rects, orientation) = self.create_split_rectangles(
            &rect,
            effective_width,
            effective_height,
            width_leftover,
            height_leftover,
        );

        self.cuts_into(&rect, placed, width_leftover, height_leftover, orientation);

        for r in new_rects {
            if r.width >= self.min_rect_size && r.height >= self.min_rect_size {
                self.remainders.push(r);
            }
        }

        // Stable sort by area, exactly as Python's `list.sort(key=area)`: the
        // ordering here is load-bearing, not cosmetic, because `place_piece`
        // breaks score ties on the FIRST matching index.
        self.remainders
            .sort_by(|a, b| a.area.partial_cmp(&b.area).unwrap());
    }

    fn cuts_into(
        &mut self,
        rect: &Rect,
        placed: &PlacedPiece,
        width_leftover: f64,
        height_leftover: f64,
        orientation: Orientation,
    ) {
        match orientation {
            Orientation::VerticalFirst => {
                if height_leftover > 0.0 {
                    self.cuts.push(Cut {
                        x: rect.x,
                        y: rect.y + placed.height,
                        length: rect.width,
                        is_horizontal: true,
                    });
                }
                if width_leftover > 0.0 {
                    self.cuts.push(Cut {
                        x: rect.x + placed.width,
                        y: rect.y,
                        length: placed.height,
                        is_horizontal: false,
                    });
                }
            }
            Orientation::HorizontalFirst => {
                if width_leftover > 0.0 {
                    self.cuts.push(Cut {
                        x: rect.x + placed.width,
                        y: rect.y,
                        length: rect.height,
                        is_horizontal: false,
                    });
                }
                if height_leftover > 0.0 {
                    self.cuts.push(Cut {
                        x: rect.x,
                        y: rect.y + placed.height,
                        length: placed.width,
                        is_horizontal: true,
                    });
                }
            }
        }
    }

    fn create_split_rectangles(
        &self,
        rect: &Rect,
        effective_width: f64,
        effective_height: f64,
        width_leftover: f64,
        height_leftover: f64,
    ) -> (Vec<Rect>, Orientation) {
        let orientation = match self.split_rule {
            SplitRule::ShorterLeftoverAxis => {
                if width_leftover <= height_leftover {
                    Orientation::VerticalFirst
                } else {
                    Orientation::HorizontalFirst
                }
            }
            SplitRule::LongerLeftoverAxis => {
                if width_leftover >= height_leftover {
                    Orientation::VerticalFirst
                } else {
                    Orientation::HorizontalFirst
                }
            }
            SplitRule::ShorterAxis => {
                if rect.width <= rect.height {
                    Orientation::VerticalFirst
                } else {
                    Orientation::HorizontalFirst
                }
            }
            SplitRule::LongerAxis => {
                if rect.width >= rect.height {
                    Orientation::VerticalFirst
                } else {
                    Orientation::HorizontalFirst
                }
            }
            SplitRule::MinimizeArea | SplitRule::MaximizeArea => {
                // Both splits are built and compared by their largest leftover;
                // this branch returns directly, it does not fall through to the
                // orientation-driven build below.
                let vertical = self.vertical_split_first(
                    rect,
                    effective_width,
                    effective_height,
                    width_leftover,
                    height_leftover,
                );
                let horizontal = self.horizontal_split_first(
                    rect,
                    effective_width,
                    effective_height,
                    width_leftover,
                    height_leftover,
                );
                let max_vertical = vertical.iter().map(|r| r.area).fold(0.0_f64, f64::max);
                let max_horizontal = horizontal.iter().map(|r| r.area).fold(0.0_f64, f64::max);
                let use_vertical = if self.split_rule == SplitRule::MinimizeArea {
                    max_vertical <= max_horizontal
                } else {
                    max_vertical >= max_horizontal
                };
                return if use_vertical {
                    (vertical, Orientation::VerticalFirst)
                } else {
                    (horizontal, Orientation::HorizontalFirst)
                };
            }
        };

        let rects = match orientation {
            Orientation::VerticalFirst => self.vertical_split_first(
                rect,
                effective_width,
                effective_height,
                width_leftover,
                height_leftover,
            ),
            Orientation::HorizontalFirst => self.horizontal_split_first(
                rect,
                effective_width,
                effective_height,
                width_leftover,
                height_leftover,
            ),
        };
        (rects, orientation)
    }

    fn vertical_split_first(
        &self,
        rect: &Rect,
        effective_width: f64,
        effective_height: f64,
        width_leftover: f64,
        height_leftover: f64,
    ) -> Vec<Rect> {
        let mut out = Vec::with_capacity(2);
        if width_leftover > 0.0 {
            let remaining_width = rect.width - effective_width;
            if remaining_width > 0.0 {
                let h = if height_leftover > 0.0 {
                    effective_height - self.kerf
                } else {
                    effective_height
                };
                out.push(Rect::new(rect.x + effective_width, rect.y, remaining_width, h));
            }
        }
        if height_leftover > 0.0 {
            let remaining_height = rect.height - effective_height;
            if remaining_height > 0.0 {
                out.push(Rect::new(
                    rect.x,
                    rect.y + effective_height,
                    rect.width,
                    remaining_height,
                ));
            }
        }
        out
    }

    fn horizontal_split_first(
        &self,
        rect: &Rect,
        effective_width: f64,
        effective_height: f64,
        width_leftover: f64,
        height_leftover: f64,
    ) -> Vec<Rect> {
        let mut out = Vec::with_capacity(2);
        if height_leftover > 0.0 {
            let remaining_height = rect.height - effective_height;
            if remaining_height > 0.0 {
                let w = if width_leftover > 0.0 {
                    effective_width - self.kerf
                } else {
                    effective_width
                };
                out.push(Rect::new(rect.x, rect.y + effective_height, w, remaining_height));
            }
        }
        if width_leftover > 0.0 {
            let remaining_width = rect.width - effective_width;
            if remaining_width > 0.0 {
                out.push(Rect::new(
                    rect.x + effective_width,
                    rect.y,
                    remaining_width,
                    rect.height,
                ));
            }
        }
        out
    }
}
