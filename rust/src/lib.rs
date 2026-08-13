//! `opticutter_core` — the guillotine packing kernel of the Cutter optimizer.
//!
//! The Python package `src/cutting/` stays in charge of the search (beam, LNS,
//! stopping rules) and of the CP-SAT endgame; what crosses into Rust is the
//! arithmetic underneath: placing pieces on one bin, splitting the leftovers
//! and emitting the cuts. Measured on the shop's real jobs, that arithmetic is
//! ~90% of the run time on pools above 120 pieces.
//!
//! **The Python implementation is the oracle.** This module must produce
//! byte-identical geometry; `scripts/bench_battery.py` digests are the
//! acceptance test, and the bridge in `src/cutting/rust_backend.py` keeps the
//! Python path alive as a fallback and a differential reference.

mod constructors;
mod models;
mod packer;

use pyo3::prelude::*;

use constructors::SortKey;
use models::{CuttingParams, Piece, Selection, SplitRule};

/// One placement: (pool index, x, y, width, height, rotated).
type PlacedOut = (u32, f64, f64, f64, f64, bool);
/// One leftover rectangle: (x, y, width, height).
type RectOut = (f64, f64, f64, f64);
/// One saw segment: (x, y, length, is_horizontal).
type CutOut = (f64, f64, f64, bool);
type FillOut = (Vec<PlacedOut>, Vec<RectOut>, Vec<CutOut>);

/// `(kerf, top_trim, bottom_trim, left_trim, right_trim)`.
type ParamsIn = (f64, f64, f64, f64, f64);
/// `(index, id_rank, width, height, can_rotate, priority)` per piece.
type PieceIn = (u32, u32, f64, f64, bool, i32);

fn build_params(p: ParamsIn) -> CuttingParams {
    CuttingParams {
        kerf: p.0,
        top_trim: p.1,
        bottom_trim: p.2,
        left_trim: p.3,
        right_trim: p.4,
    }
}

/// `index` must be the piece's position in `pieces`: `strip_fill` reads a
/// placed piece's original type back with it (see `models::Piece`).
fn build_pool(pieces: Vec<PieceIn>) -> Vec<Piece> {
    pieces
        .into_iter()
        .map(|(index, id_rank, width, height, can_rotate, priority)| {
            Piece::new(index, id_rank, width, height, can_rotate, priority)
        })
        .collect()
}

fn decode_portfolio(
    portfolio: Vec<(u8, u8, u8)>,
) -> PyResult<Vec<constructors::PortfolioEntry>> {
    portfolio
        .into_iter()
        .map(|(sort_code, split_code, selection_code)| {
            let sort_key = SortKey::from_code(sort_code)
                .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("bad sort_code"))?;
            let split_rule = SplitRule::from_code(split_code)
                .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("bad split_code"))?;
            let selection = Selection::from_code(selection_code).ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("bad selection_code")
            })?;
            Ok((sort_key, split_rule, selection))
        })
        .collect()
}

fn export(fill: Option<constructors::Fill>) -> Option<FillOut> {
    fill.map(|f| {
        (
            f.placed
                .iter()
                .map(|p| (p.index, p.x, p.y, p.width, p.height, p.rotated))
                .collect(),
            f.remainders
                .iter()
                .map(|r| (r.x, r.y, r.width, r.height))
                .collect(),
            f.cuts
                .iter()
                .map(|c| (c.x, c.y, c.length, c.is_horizontal))
                .collect(),
        )
    })
}

/// Packs one bin greedily; `None` when the bin is unusable or nothing fits.
///
/// `pieces` is `(index, id_rank, width, height, can_rotate, priority)` per
/// piece. `index` is the caller's pool position and comes back on each
/// placement; `id_rank` is the piece id's rank in the pool's lexicographic id
/// order, which is how the comparators' final `p.id` tiebreak is reproduced
/// without moving strings across the boundary.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn greedy_fill(
    pieces: Vec<PieceIn>,
    spec_width: f64,
    spec_height: f64,
    params: ParamsIn,
    sort_code: u8,
    split_code: u8,
    selection_code: u8,
    min_rect_size: f64,
) -> PyResult<Option<FillOut>> {
    let sort_key = SortKey::from_code(sort_code)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("bad sort_code"))?;
    let split_rule = SplitRule::from_code(split_code)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("bad split_code"))?;
    let selection = Selection::from_code(selection_code)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("bad selection_code"))?;

    let params = build_params(params);
    let pool = build_pool(pieces);

    Ok(export(constructors::greedy_fill(
        &pool,
        spec_width,
        spec_height,
        &params,
        sort_key,
        split_rule,
        selection,
        min_rect_size,
    )))
}

/// Packs one bin as first-stage guillotine strips; `None` when nothing fits.
///
/// Mirrors `constructors.strip_fill`: `horizontal` picks crosscut-first over
/// rip-first, `first_dim` forces the opening strip's size (the search's
/// diversity seed) and `max_repeat` caps how many identical-content strips one
/// board commits.
#[pyfunction]
#[pyo3(signature = (
    pieces, spec_width, spec_height, params,
    horizontal, first_dim, max_repeat, min_rect_size,
))]
#[allow(clippy::too_many_arguments)]
fn strip_fill(
    pieces: Vec<PieceIn>,
    spec_width: f64,
    spec_height: f64,
    params: ParamsIn,
    horizontal: bool,
    first_dim: Option<f64>,
    max_repeat: Option<u32>,
    min_rect_size: f64,
) -> PyResult<Option<FillOut>> {
    let params = build_params(params);
    let pool = build_pool(pieces);

    Ok(export(constructors::strip_fill(
        &pool,
        spec_width,
        spec_height,
        &params,
        horizontal,
        first_dim,
        max_repeat,
        min_rect_size,
    )))
}

/// Every candidate fill of one bin from one pool, deduped — `gen_fills`.
///
/// This is the boundary the bridge actually uses: one crossing per
/// `(pool, spec)` instead of ~60. `portfolio` carries the greedy configs as
/// `(sort_code, split_code, selection_code)` triples **in the caller's
/// exploration order**, which the search reshuffles under a seed.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn gen_fills(
    pieces: Vec<PieceIn>,
    spec_width: f64,
    spec_height: f64,
    params: ParamsIn,
    portfolio: Vec<(u8, u8, u8)>,
    tries: u32,
    min_rect_size: f64,
) -> PyResult<Vec<FillOut>> {
    let portfolio = decode_portfolio(portfolio)?;
    let params = build_params(params);
    let pool = build_pool(pieces);

    Ok(constructors::gen_fills(
        &pool,
        spec_width,
        spec_height,
        &params,
        &portfolio,
        tries,
        min_rect_size,
    )
    .into_iter()
    .map(|f| export(Some(f)).unwrap())
    .collect())
}

/// First probe constructor that packs the whole pool into one bin — the
/// `complete_probe` loop, collapsed into a single crossing.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn probe_fill(
    pieces: Vec<PieceIn>,
    spec_width: f64,
    spec_height: f64,
    params: ParamsIn,
    portfolio: Vec<(u8, u8, u8)>,
    target: usize,
    min_rect_size: f64,
) -> PyResult<Option<FillOut>> {
    let portfolio = decode_portfolio(portfolio)?;
    let params = build_params(params);
    let pool = build_pool(pieces);

    Ok(export(constructors::probe_fill(
        &pool,
        spec_width,
        spec_height,
        &params,
        &portfolio,
        target,
        min_rect_size,
    )))
}

#[pymodule]
fn opticutter_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(probe_fill, m)?)?;
    m.add_function(wrap_pyfunction!(greedy_fill, m)?)?;
    m.add_function(wrap_pyfunction!(strip_fill, m)?)?;
    m.add_function(wrap_pyfunction!(gen_fills, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
