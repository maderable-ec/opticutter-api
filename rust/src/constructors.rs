//! Port of the parts of `src/cutting/constructors.py` the packer needs:
//! the six sort comparators, `greedy_fill` and `strip_fill`.

use std::collections::{HashMap, HashSet};

use crate::models::{Cut, CuttingParams, Piece, PlacedPiece, Rect, Selection, SplitRule};
use crate::packer::Packer;

/// Codes for `constructors.SORT_KEYS`, in the order `GREEDY_PORTFOLIO`
/// iterates them: area, maxdim, height, width, perimeter, mindim.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SortKey {
    Area = 0,
    MaxDim = 1,
    Height = 2,
    Width = 3,
    Perimeter = 4,
    MinDim = 5,
}

impl SortKey {
    pub fn from_code(code: u8) -> Option<Self> {
        match code {
            0 => Some(SortKey::Area),
            1 => Some(SortKey::MaxDim),
            2 => Some(SortKey::Height),
            3 => Some(SortKey::Width),
            4 => Some(SortKey::Perimeter),
            5 => Some(SortKey::MinDim),
            _ => None,
        }
    }

    /// The two float slots of the comparator, after `-priority` and before the
    /// id tiebreak. Every key in `SORT_KEYS` has exactly this shape.
    #[inline]
    fn dims(&self, p: &Piece) -> (f64, f64) {
        let (w, h) = (p.width, p.height);
        let (lo, hi) = if w < h { (w, h) } else { (h, w) };
        match self {
            SortKey::Area => (-p.area, -h),
            SortKey::MaxDim => (-hi, -lo),
            SortKey::Height => (-h, -w),
            SortKey::Width => (-w, -h),
            SortKey::Perimeter => (-(w + h), -p.area),
            SortKey::MinDim => (-lo, -hi),
        }
    }
}

/// `sorted(pool, key=SORT_KEYS[...])`.
///
/// The comparator is total — every key ends in the piece id — so stability is
/// not load-bearing here, but `sort_by` is stable anyway and that matches
/// Python's `sorted`. `id_rank` stands in for the id string: it is the rank of
/// that id in the pool's lexicographic order, so comparing ranks reproduces
/// Python's string comparison without moving strings across the boundary.
pub fn sort_pool(pieces: &mut [Piece], key: SortKey) {
    pieces.sort_by(|a, b| {
        let pa = -(a.priority as f64);
        let pb = -(b.priority as f64);
        pa.partial_cmp(&pb)
            .unwrap()
            .then_with(|| {
                let (a1, a2) = key.dims(a);
                let (b1, b2) = key.dims(b);
                a1.partial_cmp(&b1)
                    .unwrap()
                    .then_with(|| a2.partial_cmp(&b2).unwrap())
            })
            .then_with(|| a.id_rank.cmp(&b.id_rank))
    });
}

pub struct Fill {
    pub placed: Vec<crate::models::PlacedPiece>,
    pub remainders: Vec<crate::models::Rect>,
    pub cuts: Vec<crate::models::Cut>,
}

/// Port of `constructors.greedy_fill`. `None` mirrors both of its exits: the
/// trims-exceed-the-bin `ValueError`, and "nothing fit".
pub fn greedy_fill(
    pool: &[Piece],
    spec_width: f64,
    spec_height: f64,
    params: &CuttingParams,
    sort_key: SortKey,
    split_rule: SplitRule,
    selection: Selection,
    min_rect_size: f64,
) -> Option<Fill> {
    let mut packer = Packer::new(
        spec_width,
        spec_height,
        params,
        split_rule,
        selection,
        min_rect_size,
    )?;

    let mut ordered: Vec<Piece> = pool.to_vec();
    sort_pool(&mut ordered, sort_key);
    packer.optimize(&ordered);

    if packer.placed.is_empty() {
        return None;
    }
    Some(Fill {
        placed: packer.placed,
        remainders: packer.remainders,
        cuts: packer.cuts,
    })
}

// ---------------------------------------------------------------------------
// strip_fill
// ---------------------------------------------------------------------------

/// Mirror of `constructors.piece_type` — geometry + constraints, hashable.
///
/// The dimensions are held as bit patterns because `f64` is not `Hash`. Every
/// dimension is strictly positive, so bit order *is* value order, and the
/// derived `Ord` therefore sorts a signature exactly like Python's
/// `sorted(counts.items())` — though only canonicality matters, since the
/// signature is used solely as the `max_repeat` bookkeeping key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
struct PieceType {
    width: u64,
    height: u64,
    can_rotate: bool,
    priority: i32,
}

type TypeSignature = Vec<(PieceType, u32)>;

#[inline]
fn piece_type(p: &Piece) -> PieceType {
    PieceType {
        width: p.width.to_bits(),
        height: p.height.to_bits(),
        can_rotate: p.can_rotate,
        priority: p.priority,
    }
}

/// Multiset of the *original* piece types behind a set of placements.
///
/// `PlacedPiece` carries the rotated dimensions, so the type has to be read off
/// the pool entry (`pool[index]`), exactly as Python reads `pp.piece`.
fn type_signature(placed: &[PlacedPiece], pool: &[Piece]) -> TypeSignature {
    let mut counts: HashMap<PieceType, u32> = HashMap::new();
    for pp in placed {
        *counts.entry(piece_type(&pool[pp.index as usize])).or_insert(0) += 1;
    }
    let mut sig: TypeSignature = counts.into_iter().collect();
    sig.sort_unstable();
    sig
}

/// Port of `_strip_candidate_dims`: distinct usable strip sizes, descending.
///
/// `sort` + `dedup` reproduces `sorted(set(dims), reverse=True)`.
fn strip_candidate_dims(pool: &[Piece], limit: f64, across: bool) -> Vec<f64> {
    let mut dims: Vec<f64> = Vec::with_capacity(pool.len() * 2);
    for p in pool {
        let (primary, secondary) = if across {
            (p.height, p.width)
        } else {
            (p.width, p.height)
        };
        if primary <= limit {
            dims.push(primary);
        }
        if p.can_rotate && secondary <= limit {
            dims.push(secondary);
        }
    }
    dims.sort_by(|a, b| b.partial_cmp(a).unwrap());
    dims.dedup();
    dims
}

/// Port of the nested `_fit_across`: the widest orientation that still fits
/// across the strip, or 0.0 when neither does.
#[inline]
fn fit_across(p: &Piece, limit: f64, across_height: bool) -> f64 {
    let (natural, rotated) = if across_height {
        (p.height, p.width)
    } else {
        (p.width, p.height)
    };
    let mut best = 0.0_f64;
    if natural <= limit {
        best = natural;
    }
    if p.can_rotate && rotated <= limit && rotated > best {
        best = rotated;
    }
    best
}

/// `(eff, used, -size) > best`, the strip's selection comparison. Strict, so
/// the FIRST candidate wins an exact tie — as in Python.
#[inline]
fn score_gt(a: (f64, f64, f64), b: (f64, f64, f64)) -> bool {
    if a.0 != b.0 {
        return a.0 > b.0;
    }
    if a.1 != b.1 {
        return a.1 > b.1;
    }
    a.2 > b.2
}

/// One strip kept as the round's winner: everything Python reads back off its
/// `best` tuple (the packer included, via its three output lists).
struct Strip {
    score: (f64, f64, f64),
    size: f64,
    placed: Vec<PlacedPiece>,
    cuts: Vec<Cut>,
    remainders: Vec<Rect>,
    signature: Option<TypeSignature>,
}

/// Port of `constructors.strip_fill`.
///
/// Fills the bin as a sequence of first-stage guillotine strips, each packed
/// recursively by `Packer` in strip-local coordinates (kerf only, no trims —
/// the trims were already consumed building `usable_w`/`usable_h`) and then
/// translated into bin coordinates.
#[allow(clippy::too_many_arguments)]
pub fn strip_fill(
    pool: &[Piece],
    spec_width: f64,
    spec_height: f64,
    params: &CuttingParams,
    horizontal: bool,
    first_dim: Option<f64>,
    max_repeat: Option<u32>,
    min_rect_size: f64,
) -> Option<Fill> {
    let kerf = params.kerf.max(0.0);
    let left = params.left_trim.max(0.0);
    let right = params.right_trim.max(0.0);
    let top = params.top_trim.max(0.0);
    let bottom = params.bottom_trim.max(0.0);
    let usable_w = spec_width - left - right;
    let usable_h = spec_height - top - bottom;
    if usable_w <= 0.0 || usable_h <= 0.0 {
        return None;
    }

    // Strips advance along `axis_room` and span `strip_span`.
    let axis_room = if horizontal { usable_h } else { usable_w };
    let strip_span = if horizontal { usable_w } else { usable_h };

    let strip_params = CuttingParams {
        kerf,
        top_trim: 0.0,
        bottom_trim: 0.0,
        left_trim: 0.0,
        right_trim: 0.0,
    };

    let mut remaining: Vec<Piece> = pool.to_vec();
    let mut all_placed: Vec<PlacedPiece> = Vec::new();
    let mut all_cuts: Vec<Cut> = Vec::new();
    let mut all_rects: Vec<Rect> = Vec::new();
    let mut cursor = 0.0_f64;
    let mut room = axis_room;
    let mut first = true;
    let mut repeat_counts: HashMap<TypeSignature, u32> = HashMap::new();

    // Reused across strip candidates so the inner loop allocates once.
    let mut fitting: Vec<Piece> = Vec::with_capacity(pool.len());

    while !remaining.is_empty() && room >= min_rect_size {
        let mut candidates = strip_candidate_dims(&remaining, room, horizontal);
        if candidates.is_empty() {
            break;
        }
        if !candidates.iter().any(|d| *d == room) {
            candidates.push(room);
        }
        if first {
            if let Some(fd) = first_dim {
                if fd <= room {
                    candidates = vec![fd];
                }
                // Cleared whether or not the seed fitted — as in Python, where
                // `first = False` sits outside the `first_dim <= room` test.
                first = false;
            }
        }

        let mut best: Option<Strip> = None;

        for &size in &candidates {
            let (mat_w, mat_h) = if horizontal {
                (strip_span, size)
            } else {
                (size, strip_span)
            };
            let mut packer = match Packer::new(
                mat_w,
                mat_h,
                &strip_params,
                SplitRule::ShorterLeftoverAxis,
                Selection::MaxEfficiency,
                min_rect_size,
            ) {
                Some(p) => p,
                None => continue,
            };

            fitting.clear();
            fitting.extend(remaining.iter().copied().filter(|p| {
                (p.width <= mat_w && p.height <= mat_h)
                    || (p.can_rotate && p.height <= mat_w && p.width <= mat_h)
            }));
            if fitting.is_empty() {
                continue;
            }

            // Widest-fit-first inside the strip (rotation-aware); the nested
            // packer still nests narrower sub-columns recursively.
            let (limit, across) = if horizontal {
                (mat_h, true)
            } else {
                (mat_w, false)
            };
            fitting.sort_by(|a, b| {
                b.priority
                    .cmp(&a.priority)
                    .then_with(|| {
                        fit_across(b, limit, across)
                            .partial_cmp(&fit_across(a, limit, across))
                            .unwrap()
                    })
                    .then_with(|| {
                        let (sa, sb) = if horizontal {
                            (a.width, b.width)
                        } else {
                            (a.height, b.height)
                        };
                        sb.partial_cmp(&sa).unwrap()
                    })
                    .then_with(|| a.id_rank.cmp(&b.id_rank))
            });

            packer.optimize(&fitting);
            if packer.placed.is_empty() {
                continue;
            }

            let signature = match max_repeat {
                Some(cap) => {
                    let sig = type_signature(&packer.placed, pool);
                    if repeat_counts.get(&sig).copied().unwrap_or(0) >= cap {
                        continue;
                    }
                    Some(sig)
                }
                None => None,
            };

            let used: f64 = packer.placed.iter().map(|p| p.width * p.height).sum();
            let score = (used / (mat_w * mat_h), used, -size);
            if best.is_none() || score_gt(score, best.as_ref().unwrap().score) {
                best = Some(Strip {
                    score,
                    size,
                    placed: packer.placed,
                    cuts: packer.cuts,
                    remainders: packer.remainders,
                    signature,
                });
            }
        }

        let best = match best {
            Some(b) => b,
            None => break,
        };

        if let Some(sig) = best.signature {
            *repeat_counts.entry(sig).or_insert(0) += 1;
        }
        let size = best.size;
        let (dx, dy) = if horizontal {
            (left, bottom + cursor)
        } else {
            (left + cursor, bottom)
        };

        // `_translate`: strip-local geometry into bin coordinates.
        all_placed.extend(best.placed.iter().map(|pp| PlacedPiece {
            index: pp.index,
            x: pp.x + dx,
            y: pp.y + dy,
            width: pp.width,
            height: pp.height,
            rotated: pp.rotated,
        }));
        all_cuts.extend(best.cuts.iter().map(|c| Cut {
            x: c.x + dx,
            y: c.y + dy,
            length: c.length,
            is_horizontal: c.is_horizontal,
        }));
        all_rects.extend(
            best.remainders
                .iter()
                .map(|r| Rect::new(r.x + dx, r.y + dy, r.width, r.height)),
        );

        let leftover = room - size;
        let advance = if leftover > 0.0 {
            // First-stage cut separating this strip from the rest of the bin.
            if horizontal {
                all_cuts.push(Cut {
                    x: left,
                    y: bottom + cursor + size,
                    length: usable_w,
                    is_horizontal: true,
                });
            } else {
                all_cuts.push(Cut {
                    x: left + cursor + size,
                    y: bottom,
                    length: usable_h,
                    is_horizontal: false,
                });
            }
            size + kerf
        } else {
            size
        };
        cursor += advance;
        room -= advance;

        let taken: HashSet<u32> = best.placed.iter().map(|pp| pp.index).collect();
        remaining.retain(|p| !taken.contains(&p.index));
    }

    if all_placed.is_empty() {
        return None;
    }

    if room >= min_rect_size {
        // Unopened tail of the bin: one continuous first-stage leftover.
        if horizontal {
            all_rects.push(Rect::new(left, bottom + cursor, usable_w, room));
        } else {
            all_rects.push(Rect::new(left + cursor, bottom, room, usable_h));
        }
    }

    Some(Fill {
        placed: all_placed,
        remainders: all_rects,
        cuts: all_cuts,
    })
}

// ---------------------------------------------------------------------------
// gen_fills
// ---------------------------------------------------------------------------

/// Port of `search._Searcher._strip_seeds`.
///
/// The trims are read raw here (not clamped at 0) because that is what the
/// Python does — `strip_fill` is the one that clamps them.
fn strip_seeds(pool: &[Piece], spec_width: f64, params: &CuttingParams) -> Vec<f64> {
    let mut dims: Vec<f64> = Vec::with_capacity(pool.len() * 2);
    for p in pool {
        dims.push(p.width);
        if p.can_rotate {
            dims.push(p.height);
        }
    }
    dims.sort_by(|a, b| b.partial_cmp(a).unwrap());
    dims.dedup();
    let usable_w = spec_width - params.left_trim - params.right_trim;
    dims.into_iter().filter(|d| *d <= usable_w).take(4).collect()
}

/// One point of the greedy portfolio, in the exploration order the caller set
/// (the search reshuffles it under a seed to surface alternative solutions).
pub type PortfolioEntry = (SortKey, SplitRule, Selection);

/// Port of `search._Searcher.gen_fills`: every candidate fill of one bin from
/// one pool, deduped by placed-piece-type multiset.
///
/// This is where the FFI boundary belongs. Crossing it was measured at 32% of
/// the Rust-side time when the bridge called `greedy_fill` once per config; one
/// call per `(pool, spec)` returning up to `tries` fills pays that toll ~60x
/// less often.
pub fn gen_fills(
    pool: &[Piece],
    spec_width: f64,
    spec_height: f64,
    params: &CuttingParams,
    portfolio: &[PortfolioEntry],
    tries: u32,
    min_rect_size: f64,
) -> Vec<Fill> {
    let mut fills: Vec<Fill> = Vec::new();
    let mut seen: HashSet<TypeSignature> = HashSet::new();
    let mut spent: u32 = 0;

    let mut push = |fills: &mut Vec<Fill>, fill: Option<Fill>| {
        let fill = match fill {
            Some(f) if !f.placed.is_empty() => f,
            _ => return,
        };
        if seen.insert(type_signature(&fill.placed, pool)) {
            fills.push(fill);
        }
    };

    let strip_budget = 6.max(tries / 2);
    let seeds = strip_seeds(pool, spec_width, params);

    for horizontal in [false, true] {
        for first_dim in std::iter::once(None).chain(seeds.iter().map(|d| Some(*d))) {
            if spent >= strip_budget {
                break;
            }
            push(
                &mut fills,
                strip_fill(
                    pool,
                    spec_width,
                    spec_height,
                    params,
                    horizontal,
                    first_dim,
                    None,
                    min_rect_size,
                ),
            );
            spent += 1;
        }
    }

    // Repeat-capped variants: spread near-perfect single-type strips across
    // boards instead of hoarding them into one (see `strip_fill`).
    for horizontal in [false, true] {
        for repeat_cap in [1u32, 2u32] {
            if spent >= strip_budget + 4 {
                break;
            }
            push(
                &mut fills,
                strip_fill(
                    pool,
                    spec_width,
                    spec_height,
                    params,
                    horizontal,
                    None,
                    Some(repeat_cap),
                    min_rect_size,
                ),
            );
            spent += 1;
        }
    }

    for &(sort_key, split_rule, selection) in portfolio {
        if spent >= tries {
            break;
        }
        push(
            &mut fills,
            greedy_fill(
                pool,
                spec_width,
                spec_height,
                params,
                sort_key,
                split_rule,
                selection,
                min_rect_size,
            ),
        );
        spent += 1;
    }

    fills
}

/// Port of the probe loop inside `search._Searcher.complete_probe`: the first
/// constructor that places the WHOLE pool (`target` pieces) into one bin.
///
/// The order is the contract — the caller's first eight greedy configs, then
/// the two strip orientations. Python builds all ten candidates and then scans
/// for the first complete one; stopping at the first hit is equivalent (the
/// constructors share no state) and is most of the point of collapsing ten FFI
/// crossings into one.
pub fn probe_fill(
    pool: &[Piece],
    spec_width: f64,
    spec_height: f64,
    params: &CuttingParams,
    portfolio: &[PortfolioEntry],
    target: usize,
    min_rect_size: f64,
) -> Option<Fill> {
    for &(sort_key, split_rule, selection) in portfolio {
        let fill = greedy_fill(
            pool,
            spec_width,
            spec_height,
            params,
            sort_key,
            split_rule,
            selection,
            min_rect_size,
        );
        if let Some(fill) = fill {
            if fill.placed.len() == target {
                return Some(fill);
            }
        }
    }
    for horizontal in [false, true] {
        let fill = strip_fill(
            pool,
            spec_width,
            spec_height,
            params,
            horizontal,
            None,
            None,
            min_rect_size,
        );
        if let Some(fill) = fill {
            if fill.placed.len() == target {
                return Some(fill);
            }
        }
    }
    None
}
