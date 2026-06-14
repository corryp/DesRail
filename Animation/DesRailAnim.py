
import pyglet
import desviz.DesViz as dv
import json

class DesVizRail:
    def __init__(self):
        with open("anim_config.json", "r") as f:
            config_file = json.load(f)
        self.window_w = config_file["window_w"]
        self.window_h = config_file["window_h"]
        self.draw_paths = config_file["draw_paths"]
        self.config_file = config_file

        self.app = dv.DesVizMaster(self.window_w, self.window_h)
        
        if not self.draw_paths:
            self.sw = self.app.add_subwindow("anim_script.json", 0, 0, self.window_w, self.window_h, pixel_units= True)
            self.sw.add_object('background', 'network.png', is_background= True)
            self.sw.set_paths('../DesRail/input/Paths.csv')
        else:
            self.sw_w = self.window_w - 2 * config_file["horiz_margin"]
            self.sw_h = self.window_h - 2 * config_file["vert_margin"]
            self.x_scale, self.y_scale = self.calculate_scale_factor()
            self.sw = self.app.add_subwindow("anim_script.json", 
                                             config_file["horiz_margin"], config_file["vert_margin"], 
                                             self.sw_w, self.sw_h, 
                                             pixel_units= False, 
                                             x_scale= self.x_scale, y_scale = self.y_scale,
                                             x0_ref = self.config_file["path_minx"],
                                             y0_ref = self.config_file["path_miny"])
            pyglet.gl.glClearColor(255, 255, 255, 1.0)
            self.sw.set_paths('../DesRail/input/Paths.csv')
            self.tracks = []
            for path_id, arc in self.sw.paths.items():
                for seg in arc.segments:
                    self.tracks.append(pyglet.shapes.Line(x= seg.x0, y= seg.y0, x2= seg.x1, y2= seg.y1, width= 1, color= (0,0,0,255), 
                                       batch= self.app.batch, group= self.sw.background))
        qut_ht, qut_scale_ht = 1200, 100
        self.sw.add_object('qut', 'qut.jpg', scale= qut_scale_ht/qut_ht, x0= self.sw.Wx-qut_scale_ht, y0= self.sw.Hy-qut_scale_ht, is_background= True)

        #create objects to highlight locked track sections
        self.lock_lines = {}
        for path_id, arc in self.sw.paths.items():
            for seg in arc.segments:
                obj_id = path_id+str(seg.seg_idx)
                self.lock_lines[obj_id] = pyglet.shapes.Rectangle(x= seg.x0, y= seg.y0, width= 3, height= seg.length, 
                                            color= (255,0,0,255), batch= self.app.batch, group= self.sw.background)
                self.lock_lines[obj_id].rotation = dv.glb_calc_rotation(seg.vect[0], seg.vect[1])
                self.lock_lines[obj_id].visible = False

        #custom command
        self.sw.custom_commands['update_arc_state'] = self.custom_command_update_arc_state     

        self.app.dock_ctrl_window(decimals= 2)
        self.app.set_anim_speed(0.05)
       
    def calculate_scale_factor(self):
        paths_w = self.config_file["path_maxx"] - self.config_file["path_minx"]
        paths_h = self.config_file["path_maxy"] - self.config_file["path_miny"]
        rescale_w = self.sw_w / paths_w
        rescale_h = self.sw_h / paths_h
        if rescale_w < rescale_h:
            #window aspect is wider than path file
            return 1.0 / paths_w, (self.sw_w / self.sw_h) / paths_w
        else:
            return (self.sw_h / self.sw_w) / paths_h, 1.0 / paths_h
        
    def custom_command_update_arc_state(self, arc_id: str, is_locked: int, r="255", g="0", b="0"):
        for seg in self.sw.paths[arc_id].segments:
            obj_id = seg.path_id+str(seg.seg_idx)
            self.lock_lines[obj_id].visible = (is_locked != "0")
            if self.lock_lines[obj_id]:
                self.lock_lines[obj_id].color = (int(r), int(g), int(b), 255)
    

def main():
    dvr = DesVizRail()
    dvr.app.run(1 / 60.0)
    
    
if __name__ == "__main__":
    main()