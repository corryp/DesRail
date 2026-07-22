#!/usr/bin/env python3
"""
Render a single frameless animation frame for the paper appendices.

Matches the user's snapshot-based export workflow: construct the viewer NORMALLY
(DesViz auto-sizes the subwindow to the path aspect, i.e. UNIFORM x/y scaling),
then resize the window and apply a UNIFORM camera (single zoom + pan) to frame
the corridor with a y-buffer. This is the key point: the camera never changes the
x/y aspect, so nothing is stretched. (An earlier version rebuilt the subwindow at
an explicit size, whose independent x/y scales distorted the corridor -- don't do
that.)

The camera is computed from the track-line extents (no interactive V snapshot
needed), with a larger margin at the TOP since auto-size leaves too little there.

Usage (run from Animation/ so sprite PNGs resolve):
  LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe /tmp/pgenv/bin/python \\
    <this> --script S.json --paths P.csv --config anim_config.json \\
    --win-w 1600 --win-h 360 --t 17 --run-in 3 --marker-radius 8 --out f.png
"""
import argparse
import os
import sys

import pyglet

# Repo-relative: <repo>/Animation, two levels up from experiments/appendix_figures/.
# Override with $DESRAIL_ANIM_DIR if this script is run from elsewhere.
ANIM_DIR = os.environ.get(
    "DESRAIL_ANIM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "Animation"))
sys.path.insert(0, ANIM_DIR)
import DesRailAnim as A             # noqa: E402

# pyglet resolves '.' relative to the main script's dir (here appendix_figures),
# not cwd; point it at Animation/ so sprite PNGs (train_blue.png, ...) resolve.
pyglet.resource.path = [ANIM_DIR]
pyglet.resource.reindex()


def fit_camera(app, tracks, win_w, win_h,
               m_left=0.03, m_right=0.03, m_top=0.22, m_bot=0.08, bbox=None):
    """Uniform camera (single zoom) that fits a pixel bbox into the window with
    fractional margins. bbox=(bx0,bx1,by0,by1) in track-pixel coords; if None,
    fits all track lines. Bigger top margin per user pref. Screen coords:
    screen = world*zoom + cam (GL y is bottom-up, so 'top' = high y)."""
    if bbox is None:
        xs, ys = [], []
        for ln in tracks:
            xs += [ln.x, ln.x2]
            ys += [ln.y, ln.y2]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    else:
        bx0, bx1, by0, by1 = bbox
    bw, bh = max(1e-9, bx1 - bx0), max(1e-9, by1 - by0)

    ml, mr = win_w * m_left, win_w * m_right
    mt, mb = win_h * m_top, win_h * m_bot
    avail_w, avail_h = win_w - ml - mr, win_h - mt - mb
    zoom = min(avail_w / bw, avail_h / bh)          # uniform => no distortion

    content_w, content_h = bw * zoom, bh * zoom
    # centre horizontally in the available band; place vertically against the
    # bottom margin (leaving the larger top margin above).
    cam_x = ml + (avail_w - content_w) / 2.0 - bx0 * zoom
    cam_y = mb - by0 * zoom
    app.cam_zoom = zoom
    app.cam_x = cam_x
    app.cam_y = cam_y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--paths", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--win-w", type=int, default=1600)
    ap.add_argument("--win-h", type=int, default=360)
    ap.add_argument("--t", type=float, required=True, help="sim time to capture")
    ap.add_argument("--run-in", type=float, default=3.0)
    ap.add_argument("--marker-radius", type=int, default=None)
    ap.add_argument("--marker-from-sprite", action="store_true",
                    help="colour each marker by its train's sprite colour")
    ap.add_argument("--m-top", type=float, default=0.22, help="top margin frac")
    ap.add_argument("--fit-bbox", default=None,
                    help="UTM 'x0,y0,x1,y1' region to frame (closeup); default = whole network")
    ap.add_argument("--out", required=True, help="output PNG path")
    args = ap.parse_args()

    dvr = A.DesVizRail(paths_file=args.paths, config_file=args.config,
                       script_file=args.script)
    app = dvr.app
    if args.marker_radius is not None:
        app.marker_radius = args.marker_radius
    if args.marker_from_sprite:
        app.marker_from_sprite = True

    # Resize the OS window (subwindow geometry stays at its uniform auto-size).
    app.set_size(args.win_w, args.win_h)
    app.flush_resize()
    bbox = None
    if args.fit_bbox:
        ux0, uy0, ux1, uy1 = [float(v) for v in args.fit_bbox.split(",")]
        px0, py0 = dvr.sw.anim_xy(ux0, uy0)
        px1, py1 = dvr.sw.anim_xy(ux1, uy1)
        bbox = (min(px0, px1), max(px0, px1), min(py0, py1), max(py0, py1))
    fit_camera(app, dvr.tracks, args.win_w, args.win_h, m_top=args.m_top, bbox=bbox)

    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)),
                           "_frames_tmp")
    app.run_export(out_dir=tmp_dir, start_sim=args.t, end_sim=args.t + 0.05,
                   speed=0.1, fps=20, make_mp4=False,
                   hide_help_bar=True, hide_ctrl=True, run_in_sim=args.run_in)
    os.replace(os.path.join(tmp_dir, "frame_0.png"), args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
