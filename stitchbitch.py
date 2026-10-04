#!/usr/bin/env python3
"""StitchBitch - Bild/SVG -> Stickdatei (.dst/.pes/.jef/.exp/.vp3/...).

Ponytail-Bauentscheidungen (bewusst, nicht vergessen):
- Kein Eigenbau der Dateiformate: pyembroidery macht sie alle. Koordinaten dort = 0,1 mm.
- Kein Eigenbau der SVG-Lib: svgelements parst Pfade/Transforms/viewBox.
- Fuellung ist eine einfache Serpentinen-/Hatch-Fuellung des Farbmasks.
  ponytail: nur eine Fuellart, keine Satin/Underlay/Pull-Compensation.
  Upgrade: Satin fuer schmale Flaechen, Underlay, Stickrichtung pro Form.
- Strichbreite in SVG wird ignoriert -> eine Laufstich-Linie.
  ponytail: keine Mehrfach-Offset-Linien fuer dicke Striche.
- Transparenz bleibt Transparenz: nur deckende Pixel (Alpha >= ALPHA_MIN) werden
  analysiert und gestickt. Weiss ist eine normale Farbe, transparent ist nichts.
- Standard: der vom Bildrand zusammenhaengende Hintergrund wird entfernt, alles
  andere (auch innenliegendes Weiss, z. B. Schrift) bekommt seinen Faden 1:1.
  --keep-background stickt auch den Hintergrund.
- Antialiasing wird vor dem Farbtraining per verbreitertem Kantenfilter
  ausgeschlossen, sonst entstehen Mischfarben als eigene Faeden (Flecken).
- Optional --outline: Randlinien je Farbe als Laufstich (gut fuer duenne
  schwarze Konturen, kann bei Schrift/Randloechern groesserer Flaechen stoeren).
- Patch-Rand (make_border/border_points): konzentrische Rechtecke um die Motiv-
  Bounding-Box, Farbe und Staerke vom User. --width zaehlt inkl. Rand.
- Stickdichte ist groessenadaptiv (auto_stitch): ~STITCHES_ACROSS Stiche ueber
  die Breite, damit jede Stickgroesse gleich detailliert ist. --stitch/--row
  ueberschreiben.
- Farb-Merge: Farben innerhalb von MERGE_PCT% (pro Kanal) werden zu einem Eintrag
  verschmolzen, dessen Wert der pixelgewichtete Durchschnitt (AVG) der Gruppe ist.
- Selbstpruefung/Selbstkorrektur: die Palette wird nur aus flaechigen Pixeln
  (ohne Antialiasing-Kanten) gelernt und die Farbanzahl automatisch erhoeht, bis
  die zugeordneten Fadenfarben das Bild treffen (Trefferquote wird ausgegeben).
  ponytail: Trefferquote = Farbtreffer pro flaechigem Pixel; kein Pixel-Diff der
  gerenderten Stiche. Upgrade waere ein gerenderter Soll/Ist-Vergleich.
- Verwendete Farben werden 1:1 als Fadenfarbe uebernommen, kein Naeherungssuchen
  in echter Garn-Palette.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import sys
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

import pyembroidery as pe
import svgelements as se

U = 10.0  # pyembroidery-Koordinaten sind 0,1 mm
MAX_RASTER = 2000  # px, Arbeitsaufloesung der Analyse (feiner = genauer, langsamer)
ALPHA_MIN = 128  # ab dieser Deckkraft zaehlt ein Pixel als Farbe, darunter = transparent
EDGE_TOL = 24  # ab diesem Farbwechsel gilt ein Pixel als Kante/Antialiasing
ACC_TOL = 40  # erlaubte Farbabweichung pro Kanal fuer "stimmt"
TARGET_ACC = 0.995  # ab dieser Trefferquote ist die 1:1-Zuordnung gut genug
COLORS_MAX = 64  # Obergrenze, bis zu der die Selbstkorrektur Farben nachlegt
MERGE_PCT = 5.0  # Farben innerhalb +/-X% pro Kanal verschmelzen (Wert = AVG der Gruppe)
# Automatische Stickdichte: Ziel ~STITCHES_ACROSS Stiche ueber die Breite, egal wie gross.
STITCHES_ACROSS = 120
MIN_STITCH, MAX_STITCH = 0.5, 3.0
MIN_ROW, MAX_ROW = 0.25, 0.5
TRIM_MM = 2.0  # laengere Spruenge werden geschnitten statt quer zu laufen


class MotifError(Exception):
    """Kein verwertbares Motiv im Bild."""


# ----------------------------------------------------------------------------- Fuellung

def _runs(idx: np.ndarray):
    """Zusammenhaengende Index-Bereiche in einem sortierten Index-Array."""
    if idx.size == 0:
        return
    cuts = np.where(np.diff(idx) != 1)[0]
    starts = np.concatenate(([0], cuts + 1))
    ends = np.concatenate((cuts, [idx.size - 1]))
    for s, e in zip(starts, ends):
        yield int(idx[s]), int(idx[e])


def fill_mask(mask, mm_per_px, stitch_mm, row_mm, angle_deg, bridge_mm=None):
    """Serpentinenfuellung eines bool-Masks (y zeigt nach unten). -> [(x_px, y_px, pen)].
    Luecken werden nur ueberbrueckt, wenn sie winzig sind (sonst Sprung -> Fadenschnitt),
    damit Fuellung nicht quer durch Loecher/Schrift stickt."""
    if bridge_mm is None:
        bridge_mm = 1.5 * row_mm
    h, w = mask.shape
    stitch_px = max(stitch_mm / mm_per_px, 1.0)
    row_px = max(row_mm / mm_per_px, 1.0)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    th = math.radians(angle_deg)
    ct, st = math.cos(th), math.sin(th)
    rad = math.hypot(w, h) / 2.0 + 2.0
    # Zeilen mit 1 px abtasten, damit die Runs exakt an der Maskengrenze enden.
    xs = np.arange(-rad, rad + 1.0, 1.0)
    ys = np.arange(-rad, rad + row_px, row_px)

    runs = []
    for j, y in enumerate(ys):
        ax = xs if j % 2 == 0 else xs[::-1]
        wx = cx + ax * ct - y * st
        wy = cy + ax * st + y * ct
        ix = np.rint(wx).astype(np.int64)
        iy = np.rint(wy).astype(np.int64)
        ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        inside = np.zeros(ax.shape, dtype=bool)
        inside[ok] = mask[iy[ok], ix[ok]]
        for s, e in _runs(np.flatnonzero(inside)):
            x0, y0, x1, y1 = float(wx[s]), float(wy[s]), float(wx[e]), float(wy[e])
            n = max(1, int(round(math.hypot(x1 - x0, y1 - y0) / stitch_px)))
            runs.append([(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)])

    out = []
    for run in runs:
        if out:
            d = math.hypot(run[0][0] - out[-1][0], run[0][1] - out[-1][1]) * mm_per_px
            if d > bridge_mm:
                out.append((run[0][0], run[0][1], 0))  # weiter Sprung -> als Jump markieren
        out.extend((x, y, 1) for x, y in run)
    return out


def _mask_contours(mask):
    """Geschlossene Randlinien einer bool-Maske (Marching Squares) in Pixelkoordinaten.
    Liefert je Rand eine geordnete Punktfolge -> als Laufstich stickbar."""
    m = np.pad(mask.astype(np.int8), 1)
    a, b = m[:-1, :-1], m[:-1, 1:]
    c, d = m[1:, 1:], m[1:, :-1]
    case = a + 2 * b + 4 * c + 8 * d
    ii, jj = np.nonzero((case != 0) & (case != 15))
    if ii.size == 0:
        return []
    cases = case[ii, jj]
    T = np.stack([2 * jj + 1, 2 * ii], axis=1)
    R = np.stack([2 * jj + 2, 2 * ii + 1], axis=1)
    B = np.stack([2 * jj + 1, 2 * ii + 2], axis=1)
    L = np.stack([2 * jj, 2 * ii + 1], axis=1)
    table = {
        1: [(L, T)], 2: [(T, R)], 3: [(L, R)], 4: [(R, B)], 5: [(L, T), (R, B)],
        6: [(T, B)], 7: [(L, B)], 8: [(B, L)], 9: [(B, T)], 10: [(T, R), (B, L)],
        11: [(B, R)], 12: [(L, R)], 13: [(T, R)], 14: [(L, T)],
    }
    adj = {}
    for k, segs in table.items():
        sel = np.nonzero(cases == k)[0]
        for p, q in segs:
            for s, e in zip(p[sel], q[sel]):
                sp, ep = (int(s[0]), int(s[1])), (int(e[0]), int(e[1]))
                adj.setdefault(sp, set()).add(ep)
                adj.setdefault(ep, set()).add(sp)

    loops = []
    while adj:
        start = next(iter(adj))
        loop, cur, seen = [start], start, {start}
        while True:
            nb = adj.get(cur)
            if not nb:
                adj.pop(cur, None)  # leere Reste entfernen, sonst Endlosschleife
                break
            nxt = nb.pop()
            adj[nxt].discard(cur)
            if not adj[cur]:
                del adj[cur]
            loop.append(nxt)
            if nxt in seen:  # geschlossen oder Zyklus erreicht -> Ende
                break
            seen.add(nxt)
            cur = nxt
        if len(loop) > 2:
            loops.append([(x / 2.0 - 1.0, y / 2.0 - 1.0) for x, y in loop])
    return loops


def _dilate(mask, radius):
    """Binärmaske um radius Pixel aufblasen (ohne Rand-Umbruch)."""
    m = mask
    for _ in range(radius):
        p = np.pad(m, 1)
        m = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    return m


def _components(mask):
    """Zusammenhaengende Komponenten einer bool-Maske (run-basiertes Union-Find).
    -> int-Labelbild, 0 = leer, 1..n = Komponenten."""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent = [0]

    def find(x):
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    nid = 1
    prev = []
    for y in range(h):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(row.astype(np.int8))
        starts = (np.flatnonzero(d == 1) + 1).tolist()
        ends = (np.flatnonzero(d == -1) + 1).tolist()
        if row[0]:
            starts.insert(0, 0)
        if row[-1]:
            ends.append(w)
        cur, pi = [], 0
        for s, e in zip(starts, ends):
            cid = 0
            while pi < len(prev) and prev[pi][1] <= s:
                pi += 1
            k = pi
            while k < len(prev) and prev[k][0] < e:
                if prev[k][1] > s:
                    if cid == 0:
                        cid = prev[k][2]
                    else:
                        ra, rb = find(cid), find(prev[k][2])
                        if ra != rb:
                            parent[max(ra, rb)] = min(ra, rb)
                k += 1
            if cid == 0:
                cid = nid
                parent.append(nid)
                nid += 1
            labels[y, s:e] = cid
            cur.append((s, e, cid))
        prev = cur

    lut = np.zeros(nid, dtype=np.int32)
    remap = {}
    for i in range(1, nid):
        r = find(i)
        if r not in remap:
            remap[r] = len(remap) + 1
        lut[i] = remap[r]
    return lut[labels]


def _drop_border_component(mask):
    """Vom Bildrand zusammenhaengende Flaechen entfernen, innenliegende behalten."""
    comp = _components(mask)
    border = np.unique(np.concatenate([comp[0], comp[-1], comp[:, 0], comp[:, -1]]))
    border = border[border > 0]
    if border.size == 0 or comp.max() == 0:
        return mask
    return mask & ~np.isin(comp, border)


def _rdp(points, eps):
    """Douglas-Peucker-Vereinfachung gegen die Pixel-Treppe der Konturen."""
    pts = np.asarray(points, dtype=float)
    if len(pts) < 3:
        return [tuple(p) for p in pts]
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        p, q = pts[i], pts[j]
        d = q - p
        seg = pts[i + 1 : j]
        length = math.hypot(d[0], d[1])
        if length == 0:
            dist = np.hypot(seg[:, 0] - p[0], seg[:, 1] - p[1])
        else:
            dist = np.abs(d[0] * (p[1] - seg[:, 1]) - d[1] * (p[0] - seg[:, 0])) / length
        k = int(np.argmax(dist)) + i + 1
        if dist.max() > eps:
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    return [tuple(p) for p in pts[keep]]


def resample(points, step):
    """Polylinie in gleiche Schrittweite zerlegen (fuer Laufstiche)."""
    if len(points) < 2:
        return list(points)
    arr = np.asarray(points, dtype=float)
    seg = np.hypot(np.diff(arr[:, 0]), np.diff(arr[:, 1]))
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    total = cum[-1]
    if total <= step:
        return [tuple(arr[0]), tuple(arr[-1])]
    n = max(1, int(total // step))
    ts = np.linspace(0.0, total, n + 1)
    xs = np.interp(ts, cum, arr[:, 0])
    ys = np.interp(ts, cum, arr[:, 1])
    out = list(zip(xs, ys))
    if math.hypot(out[-1][0] - arr[-1, 0], out[-1][1] - arr[-1, 1]) > 1e-6:
        out.append(tuple(arr[-1]))
    return out


def stroke_points(polylines_px, mm_per_px, stitch_mm):
    """Laufstiche entlang mehrerer Polylinien (y nach unten). -> [(x_px, y_px, pen)]."""
    out = []
    for pl in polylines_px:
        pts = resample(pl, max(stitch_mm / mm_per_px, 1.0))
        if not pts:
            continue
        if out:
            out.append((pts[0][0], pts[0][1], 0))
        out.extend((x, y, 1) for x, y in pts)
    return out


def border_points(x0, y0, x1, y1, width_mm, stitch_mm, row_mm):
    """Patch-Rand: konzentrische Rechtecke um die Motiv-Bounding-Box.
    width_mm = Randstaerke, gefuellt mit ringen im Abstand row_mm. -> [(x,y,pen)]."""
    width_mm = max(0.0, float(width_mm))
    if width_mm <= 0:
        return []
    step = max(row_mm, 0.05)
    stitch = max(stitch_mm, 0.2)
    n = max(1, int(round(width_mm / step)))
    out = []
    for k in range(n):
        d = step * (k + 0.5)
        rx0, ry0, rx1, ry1 = x0 - d, y0 - d, x1 + d, y1 + d
        ring = [(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1), (rx0, ry0)]
        line = resample(ring, stitch)
        if len(line) < 2:
            continue
        out.append((line[0][0], line[0][1], 0))  # Sprung zum Randanfang
        out.extend((x, y, 1) for x, y in line)
    return out


def make_border(args, report):
    """Rand-Parameter aus CLI/Web-Args bauen (None = kein Rand)."""
    color = getattr(args, "border_color", None)
    if not color:
        return None
    if not str(color).startswith("#"):
        color = "#" + str(color)
    return {
        "color": color,
        "width": float(getattr(args, "border_width", 3.0) or 0.0),
        "stitch": float(report.get("stitch_mm") or 1.0),
        "row": float(report.get("row_mm") or 0.3),
    }


def to_mm(points, mm_per_px):
    # pyembroidery rechnet selbst mit y nach unten (wie Bild/SVG); NICHT negieren,
    # sonst ist das Motiv vertikal gespiegelt.
    return [(x * mm_per_px, y * mm_per_px, pen) for x, y, pen in points]


# ----------------------------------------------------------------------------- Skalierung

def scale_for(box_w, box_h, args):
    """mm pro Quell-Einheit, damit die Grafik in width/height passt. Ein Patch-Rand
    zaehlt zur Gesamtgroesse und wird von width/height abgezogen."""
    reserve = (
        2.0 * float(getattr(args, "border_width", 0.0) or 0.0)
        if getattr(args, "border_color", None)
        else 0.0
    )
    w = args.width - reserve if args.width > 0 else math.inf
    h = args.height - reserve if args.height > 0 else math.inf
    w = max(w, 1.0) if math.isfinite(w) else w
    h = max(h, 1.0) if math.isfinite(h) else h
    s = min(w / box_w, h / box_h)
    if not math.isfinite(s):
        s = 100.0 / box_w
    return s


def hex_of(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def auto_stitch(width_mm, args):
    """Stichlaenge/Reihenabstand so waehlen, dass die Detailtreue unabhaengig von
    der Stickgroesse ist (= konstante Stichzahl ueber die Breite). Explizite
    --stitch/--row gewinnen."""
    stitch = getattr(args, "stitch", None)
    row = getattr(args, "row", None)
    if not stitch:
        stitch = min(max(width_mm / STITCHES_ACROSS, MIN_STITCH), MAX_STITCH)
    if not row:
        row = min(max(stitch / 4.0, MIN_ROW), MAX_ROW)
    return float(stitch), float(row)


# ----------------------------------------------------------------------------- Raster

def _nearest_label(flat_rgb, palette):
    """Fuer jeden Pixel Index der naechsten Palettenfarbe (speicherschonend, blockweise)."""
    pal = np.asarray(palette, dtype=np.int32)
    # Blockgroesse so, dass (block * K * 3 * 4 Byte) ~ 16 MB bleibt
    chunk = int(max(20_000, min(2_000_000, 16_000_000 / max(1, len(pal)) / 12)))
    out = np.empty(len(flat_rgb), dtype=np.int32)
    for s in range(0, len(flat_rgb), chunk):
        blk = flat_rgb[s : s + chunk].astype(np.int32)
        diff = blk[:, None, :] - pal[None, :, :]
        out[s : s + chunk] = np.square(diff).sum(-1).argmin(-1)
    return out


def _prepare(path, max_dim=MAX_RASTER):
    """Bild laden, auf Arbeitsaufloesung bringen, Alpha als Content-Maske behalten.
    -> (rgb (H,W,3), opaque (H,W) bool). Transparent wird NICHT auf Weiss gelegt."""
    img = Image.open(path)
    if max(img.size) > max_dim:
        img = img.copy()
        img.thumbnail((max_dim, max_dim))
    arr = np.asarray(img.convert("RGBA"))
    return arr[..., :3], arr[..., 3] >= ALPHA_MIN


def _edge_mask(rgb, tol=EDGE_TOL):
    """Kanten/Antialiasing: Pixel mit starkem Farbwechsel zur Nachbarschaft."""
    g = rgb.astype(np.int16)
    gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
    gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
    return np.maximum(gx.max(-1), gy.max(-1)) > tol


def _merge_colors(cols, counts, pct):
    """Farben innerhalb +/-pct% (pro Kanal) verschmelzen. Jede Gruppe wird durch
    den pixelgewichteten Durchschnitt (AVG) ihrer Mitglieder ersetzt."""
    if pct <= 0 or len(cols) <= 1:
        return cols
    tol = max(1.0, pct / 100.0 * 255.0)
    arr = np.asarray(cols, dtype=np.int32)
    groups = []
    for k in sorted(range(len(cols)), key=lambda i: -counts[i]):  # groesste zuerst
        for g in groups:
            if np.abs(arr[k] - arr[g].mean(0)).max() <= tol:
                g.append(k)
                break
        else:
            groups.append([k])
    out = []
    for g in groups:
        w = np.asarray([counts[i] for i in g], dtype=float)
        if w.sum() <= 0:
            w = np.ones(len(g))
        out.append(tuple(int(v) for v in np.round((arr[g] * w[:, None]).sum(0) / w.sum())))
    return out


def _palette_from(sample, colors, merge_pct=MERGE_PCT):
    """Median-Cut + Verfeinerung auf den uebergebenen Pixeln -> exakte Fadenfarben.
    Nur flaechige Pixel uebergeben, damit Antialiasing keine eigenen Faeden erzeugt.
    Zum Schluss werden aehnliche Farben zu ihrem Durchschnitt verschmolzen."""
    q = Image.fromarray(sample.reshape(-1, 1, 3)).quantize(max(2, colors), method=Image.MEDIANCUT)
    pal = q.getpalette()
    cols = [tuple(int(v) for v in pal[3 * i : 3 * i + 3]) for i in np.unique(np.asarray(q))]
    for _ in range(2):
        lab = _nearest_label(sample, cols)
        nxt = [
            tuple(int(round(v)) for v in sample[lab == k].mean(0)) if (lab == k).any() else cols[k]
            for k in range(len(cols))
        ]
        if nxt == cols:
            break
        cols = nxt
    lab = _nearest_label(sample, cols)
    counts = np.bincount(lab, minlength=len(cols)).tolist()
    return _merge_colors(cols, counts, merge_pct)


def _sample_accuracy(sample, cols):
    if not cols or len(sample) == 0:
        return 0.0
    lab = _nearest_label(sample, cols)
    pal = np.asarray(cols, dtype=np.int16)
    diff = np.abs(pal[lab] - sample.astype(np.int16)).max(-1)
    return float((diff <= ACC_TOL).mean())


def _finalize(rgb, opaque, cols):
    """Vollaufloesung: Pixel zuordnen, Flaechen filtern, Hintergrund finden."""
    labels = np.full(opaque.shape, -1, dtype=np.int32)
    op_rgb = rgb[opaque]
    if not cols or len(op_rgb) == 0:
        return labels, [], None
    lab = _nearest_label(op_rgb, cols)
    # letzte Verfeinerung auf allen deckenden Pixeln
    cols = [
        tuple(int(round(v)) for v in op_rgb[lab == k].mean(0)) if (lab == k).any() else cols[k]
        for k in range(len(cols))
    ]
    lab = _nearest_label(op_rgb, cols)

    # Flaechenfilter; Reihenfolge wird zum Label-Index
    counts = np.bincount(lab, minlength=len(cols))
    thr = max(20, 0.0005 * int(opaque.sum()))
    keep = [k for k in range(len(cols)) if counts[k] >= thr] or [int(counts.argmax())]
    keep.sort(key=lambda k: -counts[k])
    lut = np.full(len(cols), -1, dtype=np.int32)
    for new, old in enumerate(keep):
        lut[old] = new
    labels[opaque] = lut[lab]
    entries = [(int(counts[k]), cols[k]) for k in keep]

    border = np.zeros(opaque.shape, dtype=bool)
    border[0, :] = border[-1, :] = True
    border[:, 0] = border[:, -1] = True
    ob = labels[opaque & border]
    ob = ob[ob >= 0]
    auto_bg = None
    if ob.size:
        vals, bcnts = np.unique(ob, return_counts=True)
        bg_label = int(vals[np.argmax(bcnts)])
        if int((labels == bg_label).sum()) > 0.15 * int(opaque.sum()):
            auto_bg = hex_of(entries[bg_label][1])
    return labels, entries, auto_bg


def _accuracy(rgb, score_mask, labels, entries):
    """Anteil der flaechigen Pixel, deren Fadenfarbe zum Bild passt."""
    if not entries or not score_mask.any():
        return 0.0
    pal = np.asarray([c for _, c in entries], dtype=np.int16)
    pred = pal[labels[score_mask]]
    diff = np.abs(pred - rgb[score_mask].astype(np.int16)).max(-1)
    return float((diff <= ACC_TOL).mean())


def _raster_analysis(path, args):
    """Selbstkorrigierende 1:1-Analyse: Farbanzahl auf einer Stichprobe hochdrehen,
    dann EINMAL in Vollaufloesung zuordnen (sonst dauert das bei Fotos ewig)."""
    rgb, opaque = _prepare(path)
    if not opaque.any():
        raise MotifError("Bild ist komplett transparent.")
    # Kantenband verbreitern, sonst lecken weiche Antialiasing-Saetze in die Palette.
    flat = opaque & ~_dilate(_edge_mask(rgb), 2)
    score = flat if flat.any() else opaque
    flat_pix = rgb[score]
    step = max(1, len(flat_pix) // 250_000)
    sample = flat_pix[::step]

    merge_pct = float(getattr(args, "merge_color", MERGE_PCT) or 0.0)
    colors = max(2, min(int(args.colors), COLORS_MAX))
    cols = _palette_from(sample, colors, merge_pct)
    for _ in range(6):
        if _sample_accuracy(sample, cols) >= TARGET_ACC or colors >= COLORS_MAX:
            break
        colors = min(COLORS_MAX, colors * 2)
        cols = _palette_from(sample, colors, merge_pct)

    labels, entries, auto_bg = _finalize(rgb, opaque, cols)
    acc = _accuracy(rgb, score, labels, entries)
    return rgb, opaque, labels, entries, auto_bg, acc, colors


def _ignore_set(args, auto_bg=None):
    # Nur explizit ignorierte Farben werden komplett entfernt; der Hintergrund
    # wird separat randverbunden behandelt.
    return {_norm_hex(h) for h in (getattr(args, "bg_colors", None) or [])}


def _norm_hex(h):
    return str(h).lower().lstrip("#")


def load_raster_blocks(path, args, report=None):
    rgb, opaque, labels, entries, auto_bg, acc, used = _raster_analysis(path, args)
    if report is not None:
        report["accuracy"] = acc
        report["colors_used"] = len(entries)
    ignore = _ignore_set(args, auto_bg)  # komplett zu entfernende Farben
    # Randverbundener Hintergrund: nur die aussen liegende Flaeche dieser Farbe
    # faellt weg, innenliegende (z. B. weisse Schrift) bleiben.
    if getattr(args, "bg_color", None):
        border_bg = _norm_hex(args.bg_color)
    elif not getattr(args, "keep_bg", False) and auto_bg:
        border_bg = _norm_hex(auto_bg)
    else:
        border_bg = None

    layers = []
    for i, (c, col) in enumerate(entries):
        hx = _norm_hex(hex_of(col))
        if hx in ignore:
            continue
        m = labels == i
        if border_bg is not None and hx == border_bg:
            m = _drop_border_component(m)
        if m.any():
            layers.append((c, col, m))
    if not layers:
        raise MotifError("Keine Motive gefunden (alles Hintergrund?).")

    anymask = np.zeros(opaque.shape, dtype=bool)
    for _, _, m in layers:
        anymask |= m
    ys, xs = np.nonzero(anymask)
    box_w = int(xs.max() - xs.min()) + 1
    mm_per_px = scale_for(box_w, int(ys.max() - ys.min()) + 1, args)
    stitch_mm, row_mm = auto_stitch(box_w * mm_per_px, args)
    if report is not None:
        report["stitch_mm"] = round(stitch_mm, 2)
        report["row_mm"] = round(row_mm, 2)

    outline = getattr(args, "outline", False)
    stitch_px = max(stitch_mm / mm_per_px, 1.0)
    blocks = []
    for _, color, m in layers:
        pts = fill_mask(m, mm_per_px, stitch_mm, row_mm, args.angle)
        if outline:
            # Randlinien als Laufstich: macht duenne Konturen/Schrift solide,
            # wo die Hatch-Fuellung nur Punkte setzen wuerde.
            for loop in _mask_contours(m):
                line = resample(_rdp(loop, 1.5), stitch_px)
                if len(line) < 2:
                    continue
                pts.append((line[0][0], line[0][1], 0))  # Sprung zum Rand
                pts.extend((x, y, 1) for x, y in line)
        if pts:
            blocks.append((hex_of(color), to_mm(pts, mm_per_px)))
    return blocks


# ----------------------------------------------------------------------------- SVG

def _sample_seg(s):
    if type(s).__name__ in ("Line", "Close"):
        return [(s.start.x, s.start.y), (s.end.x, s.end.y)]
    n = 24  # ponytail: feste Kurvenaufloesung, reicht fuer Raster-/Fuellzwecke
    return [(s.point(t).x, s.point(t).y) for t in np.linspace(0.0, 1.0, n)]


def _subpaths(segs):
    subs, cur = [], None
    for s in segs:
        if type(s).__name__ == "Move":
            if cur and len(cur) > 1:
                subs.append(cur)
            cur = [(s.end.x, s.end.y)]
            continue
        pts = _sample_seg(s)
        if cur is None:
            cur = list(pts)
        else:
            cur.extend(pts[1:])
    if cur and len(cur) > 1:
        subs.append(cur)
    return subs


def _rasterize(subs_px, w, h):
    acc = np.zeros((h, w), dtype=bool)
    for sub in subs_px:
        if len(sub) < 3:
            continue
        im = Image.new("1", (w, h), 0)
        ImageDraw.Draw(im).polygon([(float(x), float(y)) for x, y in sub], fill=1)
        acc ^= np.asarray(im, dtype=bool)  # even-odd: Loecher werden ausgespart
    return acc


def load_svg_blocks(path, args, report=None):
    svg = se.SVG.parse(str(path))
    elems = []
    for e in svg.elements():
        if type(e).__name__ in ("SVG", "Group"):
            continue
        try:
            segs = e.segments()
        except Exception:
            continue
        subs = _subpaths(segs)
        if not subs:
            continue
        fill = getattr(e, "fill", None)
        stroke = getattr(e, "stroke", None)
        elems.append((subs, getattr(fill, "hex", None), getattr(stroke, "hex", None)))
    if not elems:
        raise MotifError("SVG enthaelt keine verwertbaren Pfade.")

    pts_all = [p for subs, _, _ in elems for sub in subs for p in sub]
    minx = min(p[0] for p in pts_all)
    miny = min(p[1] for p in pts_all)
    box_w = max(p[0] for p in pts_all) - minx
    box_h = max(p[1] for p in pts_all) - miny
    if box_w <= 0 or box_h <= 0:
        raise MotifError("SVG-Geometrie ist leer/entartet.")

    mpu = scale_for(box_w, box_h, args)  # mm pro SVG-Einheit
    work = int(min(max(box_w * mpu * 4, 64), 2400))  # Zielbreite in px
    ppu = work / box_w  # px pro SVG-Einheit
    w, h = int(box_w * ppu) + 3, int(box_h * ppu) + 3
    ox, oy = 1 - minx * ppu, 1 - miny * ppu
    mm_per_px = mpu / ppu

    def to_px(sub):
        return [(p[0] * ppu + ox, p[1] * ppu + oy) for p in sub]

    ignore = _ignore_set(args, None)  # SVG: kein Auto-Hintergrund, nur explizit Ignoriertes

    fills, strokes = {}, {}
    for subs, fill, stroke in elems:
        if fill and _norm_hex(fill) not in ignore:
            masks = fills.setdefault(fill, np.zeros((h, w), dtype=bool))
            masks |= _rasterize([to_px(s) for s in subs], w, h)
        if stroke and _norm_hex(stroke) not in ignore:
            strokes.setdefault(stroke, []).extend(to_px(s) for s in subs)

    stitch_mm, row_mm = auto_stitch(box_w * mpu, args)

    blocks = []
    for fill, mask in sorted(fills.items(), key=lambda kv: -int(kv[1].sum())):
        pts = fill_mask(mask, mm_per_px, stitch_mm, row_mm, args.angle)
        if pts:
            blocks.append((fill, to_mm(pts, mm_per_px)))
    # Striche zuletzt (liegen obenauf)
    for stroke, polys in strokes.items():
        pts = stroke_points(polys, mm_per_px, stitch_mm)
        if pts:
            blocks.append((stroke, to_mm(pts, mm_per_px)))
    if report is not None:
        report["accuracy"] = 1.0  # Vektor: Farben sind exakt
        report["colors_used"] = len(fills) + len(strokes)
        report["stitch_mm"] = round(stitch_mm, 2)
        report["row_mm"] = round(row_mm, 2)
    return blocks


def analyze_palette(path, args):
    """Farbpalette fuer die UI -> [{"hex": "#rrggbb", "bg": bool}]."""
    if Path(path).suffix.lower() == ".svg":
        svg = se.SVG.parse(str(path))
        counts = {}
        for e in svg.elements():
            if type(e).__name__ in ("SVG", "Group"):
                continue
            for attr in ("fill", "stroke"):
                h = getattr(getattr(e, attr, None), "hex", None)
                if h:
                    counts[h] = counts.get(h, 0) + 1
        return [{"hex": h, "bg": False} for h in sorted(counts, key=lambda k: -counts[k])]

    _, _, _, entries, auto_bg, acc, used = _raster_analysis(path, args)
    bg = _norm_hex(auto_bg) if auto_bg else None
    return [
        {"hex": hex_of(col), "bg": _norm_hex(hex_of(col)) == bg, "count": c}
        for c, col in entries
    ]


# ----------------------------------------------------------------------------- Export

def _move(pattern, x, y, limit=100.0):
    """Springe > 10 mm aufteilen - sonst crasht der DST-Writer (>121 Einheiten)."""
    last = pattern.stitches[-1] if pattern.stitches else [0.0, 0.0]
    lx, ly = last[0], last[1]
    dist = math.hypot(x - lx, y - ly)
    if dist <= limit:
        pattern.move_abs(x, y)
        return
    n = int(math.ceil(dist / limit))
    for k in range(1, n + 1):
        pattern.move_abs(lx + (x - lx) * k / n, ly + (y - ly) * k / n)


def build_pattern(blocks, rotate_deg=0.0, border=None):
    # Auf den Ursprung zentrieren: sonst blaehen die Anfahrt-Spruenge von (0,0)
    # die Bounding-Box auf.
    pts_all = [p for _, pts in blocks for p in pts]
    xs = [p[0] for p in pts_all]
    ys = [p[1] for p in pts_all]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    blocks = [(c, [(x - cx, y - cy, pen) for x, y, pen in pts]) for c, pts in blocks]

    # Gesamtes Motiv um den Ursprung drehen
    if rotate_deg:
        th = math.radians(rotate_deg)
        ct, st = math.cos(th), math.sin(th)
        blocks = [
            (c, [(x * ct - y * st, x * st + y * ct, pen) for x, y, pen in pts])
            for c, pts in blocks
        ]

    # Patch-Rand um die (gedrehte) Bounding-Box; symmetrisch, Motiv bleibt zentriert.
    if border and border.get("color") and border.get("width", 0) > 0:
        bxs = [p[0] for _, pts in blocks for p in pts]
        bys = [p[1] for _, pts in blocks for p in pts]
        ring = border_points(min(bxs), min(bys), max(bxs), max(bys),
                             border["width"], border["stitch"], border["row"])
        if ring:
            blocks.append((border["color"], ring))

    # Gleiche Farbe aufeinanderfolgend zusammenfassen
    merged = []
    for color, pts in blocks:
        if merged and merged[-1][0] == color:
            merged[-1][1].extend(pts)
        else:
            merged.append((color, list(pts)))
    merged = [(c, p) for c, p in merged if p]

    pattern = pe.EmbPattern()
    for color, _ in merged:
        pattern.add_thread(pe.EmbThread(int(color.lstrip("#"), 16), description=color))

    prev = None
    for color, pts in merged:
        if prev is not None and color != prev:
            pattern.color_change()
        prev = color
        _move(pattern, pts[0][0] * U, pts[0][1] * U)
        for x, y, pen in pts:
            if pen:
                pattern.stitch_abs(x * U, y * U)
            else:
                last = pattern.stitches[-1] if pattern.stitches else [x * U, y * U]
                # Langen Sprung (z. B. ueber Schrift) mit Fadenschnitt, sonst
                # laeuft der Faden sichtbar quer.
                if math.hypot(x * U - last[0], y * U - last[1]) / U > TRIM_MM:
                    pattern.trim()
                _move(pattern, x * U, y * U)
        pattern.trim()
    pattern.end()
    return pattern


# ----------------------------------------------------------------------------- Web-UI

FORMATS = ["dst", "pes", "jef", "exp", "vp3", "pec", "xxx", "u01"]


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/index.html"):
            page = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
            page = page.replace(
                "__FORMATS__",
                "".join(f'<option value="{f}">{f.upper()}</option>' for f in FORMATS),
            )
            self._send(200, "text/html; charset=utf-8", page.encode())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if route not in ("/convert", "/palette"):
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        name = qs.get("filename", ["upload.png"])[0]
        n = lambda k, d: float(qs.get(k, [d])[0])
        opt = lambda k: (float(qs[k][0]) if qs.get(k, [""])[0] not in ("", "auto") else None)
        # Vom Client unmarkierte Palettenfarben = zu ignorieren (Hintergrund).
        ignore = [c for c in qs.get("ignore", [""])[0].split(",") if c.strip()]
        args = SimpleNamespace(
            width=n("width", 100), height=n("height", 0), colors=int(n("colors", 16)),
            stitch=opt("stitch"), row=opt("row"), angle=n("angle", 45),
            rotate=n("rotate", 0), merge_color=n("merge", MERGE_PCT),
            bg_colors=ignore, bg_color=(qs.get("bgcolor", [""])[0] or None),
            keep_bg=n("keepbg", 0) > 0, outline=n("outline", 0) > 0,
            border_color=(qs.get("bcolor", [""])[0] or None),
            border_width=n("bwidth", 3.0),
        )
        suffix = Path(name).suffix.lower() or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            src = Path(f.name)
        try:
            if route == "/palette":
                pal = analyze_palette(src, args)
                self._send(200, "application/json", json.dumps({"colors": pal}).encode())
                return
            fmt = qs.get("format", ["dst"])[0].lstrip(".").lower()
            if fmt not in FORMATS:
                fmt = "dst"
            loader = load_svg_blocks if suffix == ".svg" else load_raster_blocks
            report = {}
            blocks = loader(src, args, report)
            pattern = build_pattern(blocks, args.rotate, make_border(args, report))
            if not pattern.stitches:
                raise ValueError("Keine Stiche erzeugt.")
            pv = io.BytesIO()
            pe.write_png(pattern, pv)
            out_tmp = None
            try:
                with tempfile.NamedTemporaryFile(suffix="." + fmt, delete=False) as g:
                    out_tmp = Path(g.name)
                pe.write(pattern, str(out_tmp))
                emb = out_tmp.read_bytes()
            finally:
                if out_tmp:
                    out_tmp.unlink(missing_ok=True)
            st = [p for p in pattern.stitches if p[2] == pe.STITCH]
            xs, ys = [p[0] for p in st], [p[1] for p in st]
            self._send(200, "application/json", json.dumps({
                "preview": base64.b64encode(pv.getvalue()).decode(),
                "file": base64.b64encode(emb).decode(),
                "filename": (Path(name).stem or "design") + "." + fmt,
                "stitches": pattern.count_stitches(),
                "width_mm": round((max(xs) - min(xs)) / U, 1),
                "height_mm": round((max(ys) - min(ys)) / U, 1),
                "blocks": pattern.count_threads(),
                "accuracy": report.get("accuracy"),
                "colors_used": report.get("colors_used"),
                "stitch_mm": report.get("stitch_mm"),
                "row_mm": report.get("row_mm"),
            }).encode())
        except Exception as exc:  # ponytail: eine Fehlermeldung fuer alles, kein Log-Framework
            self._send(400, "application/json", json.dumps({"error": str(exc)}).encode())
        finally:
            src.unlink(missing_ok=True)


def serve(args):
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"StitchBitch laeuft auf http://{args.host}:{args.port}  (Strg+C beendet)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbeendet")
    return 0


# ----------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="stitchbitch",
        description="Bild/SVG -> Stickdatei (pyembroidery-Formate).",
    )
    ap.add_argument("input", nargs="?", help="PNG/JPG/... oder SVG")
    ap.add_argument("-o", "--output", help="Zieldatei; Endung waehlt Format (.dst/.pes/.jef/...)")
    ap.add_argument("--serve", action="store_true", help="Web-UI starten statt konvertieren")
    ap.add_argument("--host", default="127.0.0.1", help="Bind-Adresse der Web-UI")
    ap.add_argument("--port", type=int, default=8000, help="Port der Web-UI")
    ap.add_argument("--width", type=float, default=100.0, help="fertige Breite in mm (Default 100)")
    ap.add_argument("--height", type=float, default=0.0, help="max. Hoehe in mm (0 = egal)")
    ap.add_argument("--colors", type=int, default=16, help="Farbanzahl fuer Rasterbilder")
    ap.add_argument("--merge-color", type=float, default=MERGE_PCT,
                    help="Farben innerhalb +/-X Prozent verschmelzen (Wert = Durchschnitt; Default 5)")
    ap.add_argument("--stitch", type=float, default=None,
                    help="Stichlaenge in mm (Default: automatisch nach Groesse)")
    ap.add_argument("--row", type=float, default=None,
                    help="Reihenabstand der Fuellung in mm (Default: automatisch nach Groesse)")
    ap.add_argument("--angle", type=float, default=45.0, help="Fuellwinkel in Grad")
    ap.add_argument("--rotate", type=float, default=0.0, help="gesamtes Motiv um Grad drehen")
    ap.add_argument("--ignore-color", action="append", metavar="RRGGBB",
                    help="Farbe nicht sticken (mehrfach moeglich)")
    ap.add_argument("--keep-background", action="store_true",
                    help="randverbundenen Hintergrund NICHT entfernen (1:1, alles sticken)")
    ap.add_argument("--outline", action="store_true",
                    help="zusaetzlich Randlinien je Farbe als Laufstich sticken (Konturen/Schrift)")
    ap.add_argument("--border-color", default=None, metavar="RRGGBB",
                    help="Patch-Rand um das Motiv (Farbe, z. B. #000000); ohne = kein Rand")
    ap.add_argument("--border-width", type=float, default=3.0,
                    help="Patch-Rand: Staerke in mm (Default 3); --width zaehlt inkl. Rand")
    ap.add_argument("--preview", help="zusaetzlich Vorschau-PNG schreiben")
    args = ap.parse_args(argv)
    args.bg_colors = args.ignore_color
    args.bg_color = None
    args.keep_bg = args.keep_background

    if args.serve:
        return serve(args)
    if not args.input:
        ap.error("input fehlt (oder --serve fuer die Web-UI)")

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"Eingabe nicht gefunden: {src}")
    out = Path(args.output) if args.output else src.with_suffix(".dst")

    loader = load_svg_blocks if src.suffix.lower() == ".svg" else load_raster_blocks
    report = {}
    try:
        blocks = loader(src, args, report)
    except MotifError as exc:
        raise SystemExit(str(exc))
    pattern = build_pattern(blocks, args.rotate, make_border(args, report))
    if not pattern.stitches:
        raise SystemExit("Keine Stiche erzeugt.")

    pe.write(pattern, str(out))
    if args.preview:
        pe.write_png(pattern, args.preview)

    st = [p for p in pattern.stitches if p[2] == pe.STITCH]
    xs = [p[0] for p in st]
    ys = [p[1] for p in st]
    acc = report.get("accuracy")
    acc_txt = f", Treffer {acc * 100:.1f}%" if isinstance(acc, float) else ""
    dens = f", Stich/Reihe {report['stitch_mm']}/{report['row_mm']} mm" if "stitch_mm" in report else ""
    print(
        f"{out}  |  {pattern.count_stitches()} Stiche, "
        f"{pattern.count_threads()} Farben{dens}{acc_txt}, "
        f"{(max(xs) - min(xs)) / U:.1f} x {(max(ys) - min(ys)) / U:.1f} mm"
    )


if __name__ == "__main__":
    sys.exit(main())
