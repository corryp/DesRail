
import argparse
import os
import pyglet
import desviz.DesViz as dv
import json
import pandas as pd

DEFAULT_PATHS_FILE = '../DesRail/input/Paths.csv'
DEFAULT_CONFIG_FILE = 'anim_config.json'
DEFAULT_SCRIPT_FILE = 'anim_script.json'
BUFFER_FRAC = 0.05      # 5% internal margin on each side
SCREEN_FRAC = 0.90      # use at most 90% of screen in each dimension


def _require_file(path: str, kind: str) -> str:
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{kind} not found: '{path}'. "
            f"Either run from a directory where this relative path resolves, "
            f"or pass an explicit --paths/--config/--script argument."
        )
    return path


class DesVizRail:
    def __init__(self, paths_file: str = DEFAULT_PATHS_FILE,
                 config_file: str = DEFAULT_CONFIG_FILE,
                 script_file: str = DEFAULT_SCRIPT_FILE):
        self.paths_file = _require_file(paths_file, "Paths CSV")
        self.config_path = _require_file(config_file, "Animation config")
        self.script_file = _require_file(script_file, "Animation script")
        with open(self.config_path, "r") as f:
            config_file = json.load(f)
        self.draw_paths = config_file["draw_paths"]
        self.true_scale_mode = config_file.get("true_scale_mode", "none")  # 'none', 'y', or 'xy'
        self.show_markers = config_file.get("show_markers", 0)
        self.config_file = config_file

        if not self.draw_paths:
            self.window_w = config_file["window_w"]
            self.window_h = config_file["window_h"]
            self.app = dv.DesVizMaster(self.window_w, self.window_h, true_scale_mode=self.true_scale_mode)
            self.app.show_markers = bool(self.show_markers)
            self.sw = self.app.add_subwindow(self.script_file, 0, 0, self.window_w, self.window_h, pixel_units= True)
            self.sw.add_object('background', 'network.png', is_background= True)
            self.sw.set_paths(self.paths_file)
        else:
            # Compute bounding box from paths data
            paths_df = pd.read_csv(self.paths_file)
            path_minx = paths_df['waypt_x'].min()
            path_maxx = paths_df['waypt_x'].max()
            path_miny = paths_df['waypt_y'].min()
            path_maxy = paths_df['waypt_y'].max()
            paths_w = path_maxx - path_minx
            paths_h = path_maxy - path_miny
            path_aspect = paths_w / paths_h

            # Size window to fit screen with matching aspect ratio
            display = pyglet.canvas.get_display()
            screen = display.get_default_screen()
            max_w = int(screen.width * SCREEN_FRAC)
            max_h = int(screen.height * SCREEN_FRAC)

            if path_aspect > max_w / max_h:
                self.window_w = max_w
                self.window_h = int(max_w / path_aspect)
            else:
                self.window_h = max_h
                self.window_w = int(max_h * path_aspect)

            help_bar_h = 22
            self.window_h += help_bar_h
            self.app = dv.DesVizMaster(self.window_w, self.window_h, true_scale_mode=self.true_scale_mode)
            self.app.show_markers = bool(self.show_markers)

            # Subwindow with 5% buffer, offset above help bar
            margin_x = int(self.window_w * BUFFER_FRAC)
            margin_y = int((self.window_h - help_bar_h) * BUFFER_FRAC) + help_bar_h
            sw_w = self.window_w - 2 * margin_x
            sw_h = self.window_h - margin_y - int((self.window_h - help_bar_h) * BUFFER_FRAC)

            # Uniform scale (aspect ratios match, so both give the same value)
            x_scale = 1.0 / paths_w
            y_scale = 1.0 / paths_h

            pyglet.gl.glClearColor(255, 255, 255, 1.0)
            self.sw = self.app.add_subwindow(self.script_file,
                                             margin_x, margin_y, sw_w, sw_h,
                                             pixel_units= False,
                                             x_scale= x_scale, y_scale= y_scale,
                                             x0_ref= path_minx, y0_ref= path_miny)
            self.sw.set_paths(self.paths_file)
            self.tracks = []
            for path_id, arc in self.sw.paths.items():
                for seg in arc.segments:
                    self.tracks.append(pyglet.shapes.Line(x= seg.x0, y= seg.y0, x2= seg.x1, y2= seg.y1, width= 1, color= (0,0,0,255),
                                       batch= self.app.batch, group= self.sw.background))

        # Register track lines for fixed-width rendering (no zoom scaling)
        if self.draw_paths:
            for line in self.tracks:
                self.app.add_fixed_width(line)

        #create objects to highlight locked track sections
        self.lock_lines = {}
        for path_id, arc in self.sw.paths.items():
            for seg in arc.segments:
                obj_id = path_id+str(seg.seg_idx)
                self.lock_lines[obj_id] = pyglet.shapes.Rectangle(x= seg.x0, y= seg.y0, width= 3, height= seg.length,
                                            color= (255,0,0,255), batch= self.app.batch, group= self.sw.background)
                self.lock_lines[obj_id].rotation = dv.glb_calc_rotation(seg.vect[0], seg.vect[1])
                self.lock_lines[obj_id].visible = False

        # Register lock lines for fixed-width rendering
        for obj_id, rect in self.lock_lines.items():
            self.app.add_fixed_width(rect)

        #custom command
        self.sw.custom_commands['update_arc_state'] = self.custom_command_update_arc_state

        # Register reset handler to hide lock lines on time skip backwards
        def reset_lock_lines():
            for obj_id, rect in self.lock_lines.items():
                rect.visible = False
        self.app.add_on_reset(reset_lock_lines)

        self.app.dock_help_bar()
        self.app.dock_ctrl_window(decimals= 2)
        self.app.set_anim_speed(0.05)
        
    def custom_command_update_arc_state(self, arc_id: str, is_locked: int, r="255", g="0", b="0"):
        for seg in self.sw.paths[arc_id].segments:
            obj_id = seg.path_id+str(seg.seg_idx)
            self.lock_lines[obj_id].visible = (is_locked != "0")
            if self.lock_lines[obj_id]:
                self.lock_lines[obj_id].color = (int(r), int(g), int(b), 255)
    

def main():
    parser = argparse.ArgumentParser(
        description="Play back a DesRail simulation animation.")
    parser.add_argument('--paths', default=DEFAULT_PATHS_FILE,
                        help=f"Path to Paths.csv (default: {DEFAULT_PATHS_FILE})")
    parser.add_argument('--config', default=DEFAULT_CONFIG_FILE,
                        help=f"Path to anim_config.json (default: {DEFAULT_CONFIG_FILE})")
    parser.add_argument('--script', default=DEFAULT_SCRIPT_FILE,
                        help=f"Path to anim_script.json (default: {DEFAULT_SCRIPT_FILE})")
    parser.add_argument('--export', metavar='DIR',
                        help='Render a clip to PNG sequence + MP4 in DIR (no interactive playback).')
    parser.add_argument('--start', type=float, default=0.0,
                        help='Sim time at clip start (default: 0).')
    parser.add_argument('--end', type=float,
                        help='Sim time at clip end (required with --export).')
    parser.add_argument('--speed', type=float,
                        help='Sim units per real-time second (default: current set_anim_speed).')
    parser.add_argument('--fps', type=int, default=12,
                        help='Frames per real-time second (default: 12).')
    parser.add_argument('--view', metavar='JSON',
                        help='Pan/zoom snapshot JSON to apply before rendering.')
    parser.add_argument('--no-mp4', action='store_true',
                        help='Skip ffmpeg preview.mp4 step.')
    parser.add_argument('--run-in', type=float, default=0.0, metavar='T',
                        help='Sim-seconds of warmup before --start (no PNGs written). '
                             'Lets per-frame state settle after the time skip; useful when '
                             'trains rendered immediately after a skip look wrong until they move.')
    args = parser.parse_args()

    if args.export and args.end is None:
        parser.error('--end is required when --export is set')
    if args.view and not os.path.isfile(args.view):
        parser.error(f"--view file not found: {args.view}")

    snap = None
    if args.view:
        with open(args.view) as f:
            snap = json.load(f)

    dvr = DesVizRail(paths_file=args.paths,
                     config_file=args.config,
                     script_file=args.script)

    # Apply snapshot AFTER construction. The subwindow geometry (track-line
    # pixel positions, x_scale/y_scale) is baked at the auto-sized __init__
    # dimensions; resizing the OS window post-hoc replicates the interactive
    # "drag-to-resize" experience under which the cam state was captured.
    if snap is not None:
        snap_w, snap_h = snap.get('width'), snap.get('height')
        if snap_w is not None and snap_h is not None:
            dvr.app.set_size(snap_w, snap_h)
            dvr.app.flush_resize()  # block until WM_SIZE applied (set_size is async on Win32)
        dvr.app.cam_x = snap['cam_x']
        dvr.app.cam_y = snap['cam_y']
        dvr.app.cam_zoom = snap['cam_zoom']

    if args.export:
        speed = args.speed if args.speed is not None else dvr.app.sim_unit_per_second
        dvr.app.run_export(out_dir=args.export,
                           start_sim=args.start,
                           end_sim=args.end,
                           speed=speed,
                           fps=args.fps,
                           view_snapshot=args.view,
                           make_mp4=not args.no_mp4,
                           run_in_sim=args.run_in)
    else:
        dvr.app.run(1 / 60.0)


if __name__ == "__main__":
    main()