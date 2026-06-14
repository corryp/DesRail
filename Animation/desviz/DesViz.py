import pyglet
from pyglet.gl import *
import math
import os
import shutil
import subprocess
import time
import pandas as pd
import semantic_version as semver
import json
from collections import deque

from .logging_core import tag_command, names_commands

READS_VERSION = "1.4.0"

def is_float(var: any) -> bool:
    try:
        float(var)
        return True
    except ValueError:
        return False

def to_colour_channel(value: int | float | str) -> int:
    if isinstance(value, str):
        if is_float(value):
            value = float(value)
        else:
            raise ValueError # Can't convert string to numeric
        
    if isinstance(value, float):
        if value <= 1: # Weakness: string-converted integer colour of exactly 1 will be treated as a 255. Uncommon usually, but can guard if it becomes a problem.
            value = int(value * 255)
        else:
            value = int(value)
    
    return value

def to_colour(r: int | float | str, g: int | float | str, b: int | float | str, a: int | float | str = 255) -> tuple[int, int, int, int]:
    return (
        to_colour_channel(r),
        to_colour_channel(g),
        to_colour_channel(b),
        # to_colour_channel(a),
    )

def glb_calc_rotation(dx, dy): # Why not atan2?
    if dx==0:
        if dy >= 0:
            rotation = 0
        else:
            rotation = 180
    else:
        angle = 180/math.pi * math.atan(dy/dx)
        if dx >= 0:
            rotation = 90-angle
        else:
            rotation = 270 - angle
    return rotation

class DesVizObject:
    """A sprite wrapper with movement logic.

    Public movement entry points:
        place(x, y)              — instant placement
        move(x1, y1, t)          — linear interpolation to (x1, y1) over t
        move_on_path(path, t, ...) — follow a DesVizPath, optionally accelerating
        place_on_path(path, end) — instant placement along a path

    Supports leader/follower chains for consist-style multi-car objects:
        a follower's position is computed from a fixed dist_from_root and
        the chain root's path_queue (its spatial footprint deque).
    Supports master/slave attachment for sprites that share motion with
    another (e.g. labels, lock indicators) via attach_to/detach.
    """
    def __init__(self, id, image, batch, group, scale=1, x0=0, y0=0, is_background=False, desv= None):
        self.id = id
        self.batch = batch
        self.is_background = is_background
        self.desv = desv
        self.sprite = pyglet.sprite.Sprite(image, batch=batch, group=group)
        self.sprite.scale = scale
        self.sprite.update(x0, y0)
        self.marker = None  # optional position marker circle
        self.true_scale = False     #this is set to true if object to be scaled to reflect its true length relative to its path
        self.true_length = 0
        # true_scale_mode inherited from master: 'none' = no auto-scaling, 'y' = scale_y only, 'xy' = uniform scale

        #parameters for the current move
        self.is_moving = False
        self.t0 = 0
        self.duration = 0
        self.x0 = x0
        self.y0 = y0
        self.dx = 0
        self.dy = 0
        self.x1 = 0
        self.y1 = 0
        self.xref = x0          #current location of front guide point
        self.yref = y0
        self.xrgp = 0           #current location of rear guide point
        self.yrgp = 0
        self.guide_x = 0
        self.guide_y = 0
        self.guide_yr = 0       #assumes guide_xr = guide_x
        self.has_rear_guide = False
        self.t_prev = 0
        
        #parameters/variables for movement with acceleration
        self.accel = 0; #acceleration on path (proportion of path per unit time2)
        self.v0 = 0;    #initial speed on path (proportion of path per unit time)
        self.accel_x = 0;   #component of acceleration in x direction
        self.accel_y = 0;   #component of acceleration in y direction
        self.v0_x = 0;      #component of initial velocity in x direction
        self.v0_y = 0;      #component of initial velocity in y direction
        self.v0_seg = 0;

        #parameters for path based move
        self.path = None
        self.path_queue = deque()  # train footprint: paths currently occupied (oldest first)
        self._prev_move_path = None  # tracks last path from move_on_path (not affected by place_on_path)
        self.dist_from_root = 0.0  # true distance from chain root's front to this follower's front
        self.path_duration = 0
        self.current_segment = 0
        self.end_segment = 0
        self.end_aplha = 1
        self.end_x = 0
        self.end_y = 0
        self.path_orient = False
        self.t_i = 0
        self.current_segments = deque()  #linked list of current line segments occupied by the object
        self.leader_obj = None           #this will be None unless this object is following another object
        self.follower_obj = None
        self.path_pos = 0

        #parameters based on master/slave sprites
        self.slaves = []    #list of sprites which are slaves to this sprite
        self.master = None  #slave's reference to the master sprite
        self.master_dx = 0  #this relates to a slave, gives the offset between master and slave origin points
        self.master_dy = 0

    #NOTE: coordinates and time are in terms of animation, not original simulation units 

    def set_true_length(self, true_length, true_scale):
        self.true_scale = true_scale;
        self.true_length = true_length

    def _apply_true_scale(self, path):
        s = ((self.true_length / path.true_length) * path.total_length) / self.sprite.image.height
        mode = self.desv.true_scale_mode if self.desv else 'none'
        if mode == 'y':
            self.sprite.scale_y = s
        elif mode == 'xy':
            self.sprite.scale = s
        # 'none': no automatic scaling

    def set_guide(self, x, y, yr= -999):
        self.guide_x = x
        self.guide_y = y
        if yr != -999:
            self.guide_yr = yr
            self.has_rear_guide = True

    def place(self, x1, y1):    #imediately position object at position x1, y1
        self.xref = x1
        self.yref = y1
        (x_offset, y_offset) = self.calc_guide_offset()
        self.sprite.update(x1+x_offset, y1+y_offset)
        if self.marker is not None:
            self.marker.x = x1
            self.marker.y = y1

        if len(self.slaves) > 0:
            self.update_slaves()
    
    def rotate(self, angle):    #imediately rotate sprite around guide point
        self.sprite.rotation = angle
        (x_offset, y_offset) = self.calc_guide_offset()
        self.sprite.update(self.xref+x_offset, self.yref+y_offset)       

    def move(self, x1, y1, t0, t, auto_orient):    #initiate move at time t0 from current location to x1, y1 with move simulation time duration t and timestep dt
        self.is_moving = True
        self.t0 = t0
        self.duration = t
        if t <= 0: # Jump instantly.
            if auto_orient and not self.has_rear_guide:
                self.sprite.rotation = self.calc_rotation(x1-self.xref, y1-self.yref)
            self.place(x1,y1)
            self.is_moving = False
        else: 
            self.dx = (x1-self.xref) / t
            self.dy = (y1-self.yref) / t
            self.x0 = self.xref
            self.y0 = self.yref
            self.x1 = x1
            self.y1 = y1
            if auto_orient and not self.has_rear_guide:
                self.sprite.rotation = self.calc_rotation(self.dx, self.dy)
            if self.accel != 0:
                if self.dx == 0:
                    self.v0_x, self.v0_y = 0, self.v0_seg if self.dy > 0 else -self.v0_seg
                    self.accel_x, self.accel_y = 0, self.accel if self.dy > 0 else -self.accel
                else:
                    theta = math.atan(self.dy / self.dx)
                    theta = theta + math.pi if self.dx < 0 else theta
                    self.v0_x, self.v0_y = self.v0_seg * math.cos(theta), self.v0_seg * math.sin(theta)
                    self.accel_x, self.accel_y = self.accel * math.cos(theta), self.accel * math.sin(theta)
            self.place(self.xref, self.yref)
            
    def accel_time(self, v0, a, dist):
        det = max(v0 * v0 + 2 * a * dist, 0)
        return (-v0 + math.sqrt(det)) / a

    def move_on_path(self, path, t0, t, auto_orient, start_current =False, end =1, _accel =0, _v0 =0):
        if self._prev_move_path is not None and self._prev_move_path != path:
            self.path_queue.append(self._prev_move_path)
        self._prev_move_path = path
        self.path = path
        self.path_orient = auto_orient
        self.end_alpha = end
        self.accel = path.total_length * _accel
        self.v0 = path.total_length * _v0;
        self.nrm_accel = _accel
        self.nrm_v0 = _v0
        self.v0_seg = self.v0

        if self.true_scale:
            self._apply_true_scale(path)

        #find where along the path we are starting
        self.current_segment = 0
        if start_current:
            xy_rel_pos, st_seg = path.calc_rel_pos(self.xref, self.yref)
            self.current_segment = st_seg.seg_idx
            self.path_pos = xy_rel_pos
            self.start_alpha = xy_rel_pos
        else:
            xy_rel_pos = 0        #xy_rel_pos is relative to the entire path
            self.path_pos = 0
            self.start_alpha = 0
            self.current_segments.clear()
            st_seg = path.segments[0]

        if end == 1:
            self.end_segment = len(path.segment_lengths) - 1
            self.end_x = path.waypoints[-1][0]
            self.end_y = path.waypoints[-1][1]
            end_seg = path.segments[-1]
        else:
            self.end_x, self.end_y, end_seg = path.calc_xy(end)
            self.end_segment = end_seg.seg_idx

        self.path_duration = t
        self.duration = self.path_duration
        if not start_current:
            self.place(path.waypoints[0][0], path.waypoints[0][1])
        

        if len(self.current_segments) == 0:
            self.current_segments.append(path.segments[self.current_segment])
        elif self.current_segments[-1] != path.segments[self.current_segment]:
            self.current_segments.append(path.segments[self.current_segment])

        self.is_moving = True
        self.t0 = t0
        #if path.path_id == "(7-6)" and self.id == "train0r" and self.accel == 0:
        #    debug = 1
        

    def place_on_path(self, path, auto_orient, end =0):
        if self.true_scale:
            self._apply_true_scale(path)

        self.is_moving = False
        self.accel = 0
        
        x, y, seg = path.calc_xy(end)
        if end > 0:
            if auto_orient and not self.has_rear_guide:
                self.sprite.rotation = self.calc_rotation(seg.vect[0], seg.vect[1])
            if self.has_rear_guide and len(self.current_segments) == 0:
                self.current_segments.append(seg)
                self.rotate_to_rear_guide(x, y)            

        self.path = path
        self.place(x,y)

    def attach_to(self, master_sprite, master_dx, master_dy):
        self.master = master_sprite
        self.master.slaves.append(self)
        self.master_dx = master_dx
        self.master_dy = master_dy
        self.master.update_slaves()

    def detach(self):
        (x_offset,y_offset) = self.calc_guide_offset()
        self.xref = self.sprite.x - x_offset
        self.yref = self.sprite.y - y_offset
        self.master.slaves.remove(self)
        self.master = None

    def update_follower_position(self):
        #this will be called in update frame and from here as well
        if self.leader_obj == None:
            return

        if self.leader_obj.path == None:
            return

        # Find chain root (loco) — owns the train's path footprint
        chain_root = self.leader_obj
        while chain_root.leader_obj is not None:
            chain_root = chain_root.leader_obj

        # Compute root's absolute distance from start of oldest deque path
        root_path = chain_root.path
        if root_path is None:
            return
        root_rpos, _ = root_path.calc_rel_pos(chain_root.xref, chain_root.yref)
        root_abs_dist = sum(p.true_length for p in chain_root.path_queue) + root_rpos * root_path.true_length

        # Follower's absolute distance
        follower_abs_dist = root_abs_dist - self.dist_from_root

        # Walk forward through deque to find follower's path and position
        old_path = self.path
        accumulated = 0.0
        target_path = None
        for p in chain_root.path_queue:
            if accumulated + p.true_length >= follower_abs_dist:
                target_path = p
                f_pos = (follower_abs_dist - accumulated) / p.true_length
                break
            accumulated += p.true_length

        if target_path is None:
            # Follower is on the root's current path
            target_path = root_path
            f_pos = (follower_abs_dist - accumulated) / root_path.true_length

        if target_path != self.path:
            if self.true_scale:
                self._apply_true_scale(target_path)
            self.path = target_path

        # Tail car: clean up paths no longer occupied by any car
        if self.follower_obj is None and old_path is not None and old_path != self.path:
            while len(chain_root.path_queue) > 0 and chain_root.path_queue[0] != self.path:
                chain_root.path_queue.popleft()

        f_pos = max(0.0, min(1.0, f_pos))
        x, y, seg = self.path.calc_xy(f_pos)
        if len(self.current_segments) == 0 or self.current_segments[-1] != seg:
            self.current_segments.append(seg)
        self.rotate_to_rear_guide(x, y)
        self.place(x, y)
        if self.follower_obj != None:
            self.follower_obj.update_follower_position()

    def follow_leader(self, leader_obj):
        self.is_moving = False
        self.leader_obj = leader_obj
        self.leader_obj.follower_obj = self
        self.dist_from_root = leader_obj.dist_from_root + leader_obj.true_length
        self.update_follower_position()

    def unfollow_leader(self):
        self.leader_obj = None
        self.leader_obj.follower_obj = None

    def calc_rotation(self, dx, dy): # Why not atan2?
        if dx==0:
            if dy >= 0:
                rotation = 0
            else:
                rotation = 180
        else:
            angle = 180/math.pi * math.atan(dy/dx)
            if dx >= 0:
                rotation = 90-angle
            else:
                rotation = 270 - angle
        return rotation
       
    def calc_guide_offset(self):
        if self.guide_x == 0 and self.guide_y == 0:
            return (0,0)
        theta = math.radians(-self.sprite.rotation)
        #return(-self.sprite.scale * (self.guide_x * math.cos(theta) - self.guide_y * math.sin(theta)),
        #       -self.sprite.scale * (self.guide_x * math.sin(theta) + self.guide_y * math.cos(theta)))
        gx, gy = self.sprite.scale * self.sprite.scale_x * self.guide_x, self.sprite.scale * self.sprite.scale_y * self.guide_y
        return( -(gx * math.cos(theta) - gy * math.sin(theta)),
               -(gx * math.sin(theta) + gy * math.cos(theta)))
    
    def update_slaves(self):
        m_scale = self.sprite.scale
        m_x = self.sprite.x
        m_y = self.sprite.y
        m_th = math.radians(-self.sprite.rotation)
        for slave in self.slaves:
            #s_x = m_x + m_scale*(slave.master_dx * math.cos(m_th) - slave.master_dy * math.sin(m_th))
            #s_y = m_y + m_scale*(slave.master_dx * math.sin(m_th) + slave.master_dy * math.cos(m_th))
            s_x = m_x + (slave.master_dx * math.cos(m_th) - slave.master_dy * math.sin(m_th))
            s_y = m_y + (slave.master_dx * math.sin(m_th) + slave.master_dy * math.cos(m_th))
            slave.sprite.update(x= s_x, y= s_y, rotation= self.sprite.rotation)
    
    def rotate_to_rear_guide(self, x, y):
        if not self.has_rear_guide:
            return
        
        delta = self.sprite.scale * (self.guide_y - self.guide_yr)
        seg = self.current_segments[0]
        if len(self.current_segments) == 1:
            a = delta / self.current_segments[0].length
            self.xrgp, self.yrgp = x - a * seg.vect[0], y - a * seg.vect[1]
        else:
            seg_dist = math.sqrt((x-seg.x1)**2 + (y-seg.y1)**2)
            while seg_dist > delta and len(self.current_segments) > 1:
                self.current_segments.popleft()
                seg = self.current_segments[0]
                seg_dist = math.sqrt((x-seg.x1)**2 + (y-seg.y1)**2)
            if len(self.current_segments) == 1:
                a = delta / self.current_segments[0].length
                self.xrgp, self.yrgp = x - a * seg.vect[0], y - a * seg.vect[1]
            else:
                bseg = seg
                fseg = self.current_segments[-1]
                dx, dy = fseg.x0 - x, fseg.y0 - y
                for seg in reversed(list(self.current_segments)[1:-1]):
                    dx -= seg.vect[0]
                    dy -= seg.vect[1]
                dxn, dyn = -bseg.vect[0], -bseg.vect[1]
                determ = max((dx*dxn + dy*dyn)**2 - (dxn**2 + dyn**2)*(dx**2 + dy**2 - delta**2), 0)
                a = (-(dx*dxn + dy*dyn) + math.sqrt( determ) ) / (dxn**2 + dyn**2)
                self.xrgp, self.yrgp = x + dx + a*dxn, y + dy + a*dyn
                
        r_prev = self.sprite.rotation   #for debugging
        
        self.sprite.rotation = self.calc_rotation(x - self.xrgp, y - self.yrgp)

        #if self.path.path_id == "(4-4)":
        #    if abs(self.sprite.rotation - r_prev) > 20:
        #        debug = 1
        #if abs(self.sprite.rotation - r_prev) > 0.00005:
            #print(self.desv.sim_clock, self.path.segments[0].path_id, self.path.segments[0].seg_idx, r_prev, self.sprite.rotation)
        
    def frame_update(self, elapsed, dt):
        if not self.is_moving:
            return
        
        delta_t = self.desv.sim_clock - self.t0
        
        if delta_t >= self.path_duration:
            self.is_moving = False
            return
        
        #now update move and path status                   
        if self.accel == 0:
            if self.path != None:
                self.path_pos = min(self.end_alpha, self.start_alpha + (delta_t / self.path_duration) * (self.end_alpha - self.start_alpha))
            else:
                if self.dx > 0:
                    x = min(self.x1, self.x0 + delta_t*self.dx)
                else:
                    x = max(self.x1, self.x0 + delta_t*self.dx)
                if self.dy > 0:
                    y = min(self.y1, self.y0 + delta_t*self.dy)
                else:
                    y = max(self.y1, self.y0 + delta_t*self.dy)
        else:
            if self.path != None:
                self.path_pos = min(self.end_alpha, 
                                    self.start_alpha + self.nrm_v0 * delta_t  + 
                                    0.5 * self.nrm_accel * delta_t * delta_t)                                          
            else:
                if self.dx > 0:
                    x = self.x0 + self.v0_x * delta_t + 0.5 * self.accel_x * delta_t * delta_t
                    x = min(self.x1, x)
                else:
                    x = self.x0 + self.v0_x * delta_t + 0.5 * self.accel_x * delta_t * delta_t
                    x = max(self.x1, x)
                if self.dy > 0:
                    y = self.y0 + self.v0_y * delta_t + 0.5 * self.accel_y * delta_t * delta_t
                    y = min(self.y1, y)
                else:
                    y = self.y0 + self.v0_y * delta_t + 0.5 * self.accel_y * delta_t * delta_t
                    y = max(self.y1, y)               
        if self.path != None:
            x, y, seg = self.path.calc_xy(self.path_pos, self.current_segment)
            if seg.seg_idx != self.current_segment:
                for i in range(self.current_segment + 1, seg.seg_idx + 1):
                    self.current_segments.append(self.path.segments[i])
                self.current_segment = seg.seg_idx
                if self.path_orient and not self.has_rear_guide:
                    self.sprite.rotation = self.calc_rotation(seg.x1-seg.x0, seg.y1-seg.y0) 
                
        self.rotate_to_rear_guide(x, y)
        self.place(x,y)
        
        if self.follower_obj != None:
            self.follower_obj.update_follower_position()

        #if self.path == None:
        #    print (self.desv.sim_clock, "NULL", x, y, self.sprite.rotation)
        #elif self.path.path_id == "(24-19)":
        #elif self.path.path_id == "(9-9)":
        #    print (self.desv.sim_clock, self.path.path_id, x, y, self.sprite.rotation, self.path_pos, self.current_segment)
            #if abs(self.sprite.rotation-90)<0.0005:
                #debug = 1


class PathSegment:
    def __init__(self, x0, y0, x1, y1, path_id, seg_idx, pred_length):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.vect = (x1 - x0, y1 - y0)
        self.length = math.sqrt((x1 - x0)**2 + (y1 - y0)**2)
        self.path_id = path_id
        self.seg_idx = seg_idx
        self.pred_length = pred_length
        self.end_length = pred_length + self.length
        

class DesVizPath:
    """A polyline path built from waypoints, used for path-following motion.

    add_waypoint(x, y) appends a waypoint and creates a PathSegment between
    consecutive waypoints. calc_xy(rel_pos) maps a normalised position in
    [0, 1] to a (x, y, segment) triple along the path; calc_rel_pos(x, y)
    is the inverse (raises ValueError if (x, y) is not on the path).

    true_length carries the real-world length of the path for length-
    proportional sprite scaling — the ratio path.total_length / path.true_length
    is the scale factor used by DesVizObject.move_on_path.
    """
    def __init__ (self, a_path_id):
        self.path_id = a_path_id
        self.waypoints = []
        self.segment_lengths = []
        self.segments = []
        self.total_length = 0
        self.true_length = 0        #this is set if objects to be scaled by true length
    
    def add_waypoint(self, a_x, a_y):
        if len(self.waypoints) > 0:
            d = math.sqrt((a_x - self.waypoints[-1][0])**2 + (a_y - self.waypoints[-1][1])**2)
            self.segment_lengths.append(d)
            self.segments.append(PathSegment(self.waypoints[-1][0], self.waypoints[-1][1], a_x, a_y, self.path_id, len(self.segments), self.total_length))
            self.total_length += d
        self.waypoints.append((a_x, a_y))
    
    def calc_rel_pos(self, x, y):
        if not self.segments:
            raise ValueError(f"DesVizPath '{self.id}': cannot locate ({x},{y}) on a path with no segments")
        xy_st_rpos = 0
        xy_seg_frac = None
        matched_seg = None
        for seg in self.segments:
            if abs(seg.x0 - seg.x1) >= abs(seg.y0 - seg.y1):    #choose most numerically direction stable to work with
                xy_seg_frac = (x - seg.x0) / seg.vect[0]
                if 0 <= xy_seg_frac and xy_seg_frac <= 1:
                    y_interp = seg.y0 + xy_seg_frac * seg.vect[1]
                    if abs(y_interp - y) < 1:
                        matched_seg = seg
                        break  #(x,y) is on the line segment within 1 pixel tolerance
            else:
                xy_seg_frac = (y - seg.y0) / seg.vect[1]
                if 0 <= xy_seg_frac and xy_seg_frac <= 1:
                    x_interp = seg.x0 + xy_seg_frac * seg.vect[0]
                    if abs(x_interp - x) < 1:
                        matched_seg = seg
                        break  #(x,y) is on the line segment within 1 pixel tolerance
            xy_st_rpos += seg.length / self.total_length
        if matched_seg is None:
            raise ValueError(
                f"DesVizPath '{self.id}': ({x},{y}) does not lie on any path segment "
                f"(within 1px tolerance). Check that the caller is querying against the correct path.")
        xy_st_rpos += xy_seg_frac * matched_seg.length / self.total_length
        return xy_st_rpos, matched_seg
    
    def calc_xy(self, rel_pos, start_seg =0):
        if rel_pos == 0:
            return self.waypoints[0][0], self.waypoints[0][1], self.segments[0]
        elif rel_pos == 1:
            return self.waypoints[-1][0], self.waypoints[-1][1], self.segments[-1]
        else:
            seg_st_rpos = self.segments[start_seg].pred_length / self.total_length
            for i in range(start_seg, len(self.segments)):
                seg = self.segments[i]
                seg_end_rpos = seg_st_rpos + seg.length / self.total_length
                if seg_st_rpos < rel_pos and rel_pos <= seg_end_rpos:
                    break
                seg_st_rpos = seg_end_rpos               
            if seg_st_rpos == seg_end_rpos:
                return self.waypoints[-1][0], self.waypoints[-1][1], self.segments[-1]
            else:
                x = seg.x0 + (rel_pos - seg_st_rpos) * (seg.x1 - seg.x0) / (seg_end_rpos - seg_st_rpos)
                y = seg.y0 + (rel_pos - seg_st_rpos) * (seg.y1 - seg.y0) / (seg_end_rpos - seg_st_rpos)
                return x, y, seg
        

class DesVizProgressBar:
    def __init__(self, id, x0, y0, Wx, Hy, rotation, colour, batch, group):
        self.prog_id = id
        self.x0 = x0
        self.y0 = y0
        self.Wx = Wx
        self.Hy = Hy
        self.rotation = rotation
        self.colour = colour
        self.batch = batch
        self.group = group

        self.frame = [None]*4
        self.bar = None

        self.master = None      #sprite that the progress bar is attached to
        self.master_dx = 0
        self.master_dy = 0

        self.create_frame()
    
    def create_frame(self):
        self.bar = pyglet.shapes.Rectangle(x= self.x0, y= self.y0, width= self.Wx, height= self.Hy, 
                                            color= (self.colour[0],self.colour[1],self.colour[2],255), batch= self.batch, group= self.group)
        self.bar.rotation = self.rotation
        vertices = [0]*4
        self.get_vertices(vertices)

        for i in range(3):
            self.frame[i] = pyglet.shapes.Line(x= vertices[i][0], y= vertices[i][1], x2= vertices[i+1][0], y2= vertices[i+1][1],
                                               width= 3, color= (0,0,0,255), batch= self.batch, group= self.group)
        self.frame[3] = pyglet.shapes.Line(x= vertices[3][0], y= vertices[3][1], x2= vertices[0][0], y2= vertices[0][1],
                                               width= 3, color= (0,0,0,255), batch= self.batch, group= self.group)

    def get_vertices(self, vertices_list):
        theta = math.radians(-self.bar.rotation)
        cos_theta = math.cos(theta)
        sin_theta = math.sin(theta)
        vertices_list[0] = (self.x0, self.y0) ##
        vertices_list[1] = (self.x0 + self.Wx*cos_theta, self.y0 + self.Wx*sin_theta)
        vertices_list[2] = (self.x0 + self.Wx*cos_theta - self.Hy*sin_theta, self.y0 + self.Wx*sin_theta + self.Hy*cos_theta)
        vertices_list[3] = (self.x0 - self.Hy*sin_theta, self.y0 + self.Hy*cos_theta)
        return (cos_theta, sin_theta)

    def update_level(self, level):  #level \in [0,1]
        self.bar.width = level * self.Wx

    def update_position(self, x0, y0, rotation):
        #TODO...
        pass

    #(x_offset, y_offset) gives the offset of the object origin to the progress bar origin
    def attach_to_object(self, obj: DesVizObject, x_offset, y_offset, rotation):
        self.master = obj
        self.master_dx = x_offset
        self.master_dy = y_offset
        self.rotation = rotation

    def update_attached_position(self):
        self.bar.rotation = self.master.sprite.rotation + self.rotation
        theta = math.radians(-self.master.sprite.rotation)
        cos_theta = math.cos(theta)
        sin_theta = math.sin(theta)        
        x = self.master.sprite.x + self.master.sprite.scale*(self.master_dx*cos_theta - self.master_dy*sin_theta)
        y = self.master.sprite.y + self.master.sprite.scale*(self.master_dx*sin_theta + self.master_dy*cos_theta)
        (self.bar.x, self.bar.y) = (x, y)
        (self.x0, self.y0) = (x,y)
        vertices = [0]*4
        self.get_vertices(vertices)
        for i in range(3):
            (self.frame[i].x, self.frame[i].y) = (vertices[i][0], vertices[i][1])
            (self.frame[i].x2, self.frame[i].y2) = (vertices[i+1][0], vertices[i+1][1])
        (self.frame[3].x, self.frame[3].y) = (vertices[3][0], vertices[3][1])
        (self.frame[3].x2, self.frame[3].y2) = (vertices[0][0], vertices[0][1])        


#(x0,y0) pixel coordinates on the animation window of the origin of this subwindow
#(Wx,Hy) width and height in pixels of this subwindow
#(x0_ref, y0_ref) origin of the subwindow relative to an external coordinate system
#(x_scale, y_scale) subwindow zoom factor
#render_layer: order in which sub windows are drawn (smaller values on the bottom layers)
# If using pixel units, window is [width] pixels wide and [height] pixels high, and is not zoomed or scaled.
@names_commands
class SubWindow:
    """A coordinate-transformed viewport within a DesVizMaster window.

    Owns objects, paths, labels, and the animation script for one render
    layer. anim_xy(x_ext, y_ext) maps external (model) coordinates to
    window pixels; the transform is configured via x_scale, y_scale,
    x0_ref, y0_ref. Two modes:

      pixel_units=True  — identity transform (typically with a background
                          image at native pixels)
      pixel_units=False — scaled transform (typically UTM coordinates from
                          Paths.csv mapped to the window)

    Animation script execution: set_script() loads anim_script.json or
    .csv; update_animation_actions() (called each frame) executes every
    command whose time has arrived. Commands are dispatched via
    @tag_command-registered methods or via custom_commands.

    Extension point: register a domain-specific command by inserting into
    self.custom_commands[alias] = handler. DesRailAnim.py uses this for
    update_arc_state to colour locked track sections.
    """
    named_commands = {} # Hacky workaround for now. Will be improved later. (TODO)

    def __init__(self, master: "DesVizMaster", x0: float, y0: float, Wx: float, Hy: float, *, x0_ref: float | None = 0, y0_ref: float | None = 0, x_scale: float | None = 1, y_scale: float | None = 1, render_layer: int, name: str = '', pixel_units: bool = False):
        self.master = master

        # Pyglet coordinates
        self.x0, self.y0 = x0, y0
        self.Wx, self.Hy = Wx, Hy

        # Animation script coordinates
        self.pixel_units: bool = pixel_units
        self.x0_ref, self.y0_ref = x0_ref, y0_ref
        if self.pixel_units:
            self.x_scale, self.y_scale = 1,1
        else:
            self.x_scale, self.y_scale = Wx * x_scale, Hy * y_scale

        self.render_layer = render_layer
        self.name = name

        self.batch = master.batch
        self.marker_group = pyglet.graphics.Group(order=2*render_layer - 1)  # below background
        self.background = pyglet.graphics.Group(order=2*render_layer)
        self.foreground = pyglet.graphics.Group(order=2*render_layer + 1)
        self.all_obj = {}
        self.fore_obj = {}      #subset of all_obj including all foreground objects
        self.labels = {}
        self.paths = {}
        self.named_points: dict[str, tuple[float, float]] = {}
        self.progress_bars = {}

        #devs can put their own command here {'command_str': custom_function)}
        #arguments passed to custom_function will be (sim_t, self)
        self.custom_commands = {}   
        
        self.script = None
        self.script_type = None
        self.script_index = -1
        self.sim_t = 0

        
    #returns global animation window coordinates for external coordinates (x_extern, y_extern)
    def anim_xy(self, x_extern, y_extern):
        return self.x0 + self.x_scale*(x_extern - self.x0_ref), self.y0 + self.y_scale*(y_extern - self.y0_ref)

    def set_script(self, script_fname):
        if str.endswith(script_fname, ".csv"):
            self.script_type = "CSV"
            try:
                self.script = pd.read_csv(script_fname)
            except FileNotFoundError:
                raise FileNotFoundError(f"Animation script not found: {script_fname}")
            self.script_index = 0
            self.metadata = None
            self.script_format_version = "0.1.0"
            # TODO: Recognise "META" and "VERSION" commands if we decide to keep this.
        elif str.endswith(script_fname, ".json"):
            self.script_type = "JSON"
            try:
                with open(script_fname, "r") as f:
                    script_file = json.load(f)
            except FileNotFoundError:
                raise FileNotFoundError(f"Animation script not found: {script_fname}")
            except json.JSONDecodeError as e:
                raise ValueError(f"Animation script {script_fname} is not valid JSON: {e}") from e
            for required in ("commands", "metadata", "version"):
                if required not in script_file:
                    raise ValueError(f"Animation script {script_fname} is missing required key '{required}'")
            self.script = script_file["commands"]
            self.metadata = script_file["metadata"]
            self.script_format_version = script_file["version"]
            
            if semver.compare(self.script_format_version, READS_VERSION) != 0:
                if semver.compare(self.script_format_version, READS_VERSION) < 0:
                    print(f"Version mismatch for script {script_fname}:","Old script. Back-compatibility is possible, but not guaranteed.")
                else:
                    print(f"Version mismatch for script {script_fname}:","Old reader. Expect some things to go wrong, but we'll try anyway and see how far we get.")

            if semver.compare(self.script_format_version, "1.2.0") >= 0:
                # Script should include subwindow coordinate-sizing information. Respect overrides in args.
                self.x0_ref = self.x0_ref if self.x0_ref is not None else self.get_from_script_metadata("x0", type=float)
                self.y0_ref = self.y0_ref if self.y0_ref is not None else self.get_from_script_metadata("y0", type=float)

                if not self.pixel_units:
                    self.x_scale /= self.get_from_script_metadata("width" , default=1, type=float)
                    self.y_scale /= self.get_from_script_metadata("height", default=1, type=float)

            self.script_index = 0
    
    def get_from_script_metadata(self, key: str, default = ..., type: None|str|int|float = None):
        try:
            return self.metadata[key] if type is None else type(self.metadata[key])
        except KeyError:
            if default is not ...:
                return default
            
            assert False, "Could not repair missing data."

    @property
    def current_row(self):
        if self.script_index < 0:
            return None

        assert self.script_type in ["CSV", "JSON"]

        if self.script_type == "CSV":
            return self.script.iloc[self.script_index]
        
        if self.script_type == "JSON":
            return self.script[self.script_index]["data"]

    @property
    def current_time(self) -> float:
        return self.sim_t
    
    def set_paths(self, path_fname):
        try:
            df = pd.read_csv(path_fname)
        except FileNotFoundError:
            raise FileNotFoundError(f"Paths file not found: {path_fname}")
        required_cols = {'path_id', 'waypt_x', 'waypt_y'}
        missing = required_cols - set(df.columns)
        if missing:
            raise ValueError(
                f"Paths file {path_fname} is missing required columns {sorted(missing)}. "
                f"Found columns: {list(df.columns)}")
        prev_path = ''
        for index, row in df.iterrows():
            path_id = row['path_id']
            if path_id != prev_path:
                prev_path = path_id
                self.paths[path_id] = DesVizPath(path_id)
            x,y = self.anim_xy(row['waypt_x'], row['waypt_y'])
            self.paths[path_id].add_waypoint(x, y)

    def add_object(self, id, image_fname, scale=1, x0=0, y0=0, is_background=False):
        if id in self.all_obj:
            return
        
        x,y = self.anim_xy(x0, y0)

        if image_fname not in self.master.images:
            self.master.images[image_fname] = pyglet.resource.image(image_fname)
        image = self.master.images[image_fname]

        if not is_background:
            group = self.foreground
        else:
            group = self.background
        
        new_object = DesVizObject(id, image, self.batch, group, scale, x, y, is_background,self.master)
        self.all_obj[id] = new_object
        if not is_background:
            self.fore_obj[id] = new_object
            if self.master.show_markers:
                base_r = self.master.marker_radius
                mcol = self.master.marker_colour
                if self.master.marker_from_sprite:
                    mcol = self.master.sprite_avg_colour(image_fname, image)
                new_object.marker = pyglet.shapes.Circle(x, y, base_r / self.master.cam_zoom,
                                                         color=mcol,
                                                         batch=self.batch, group=self.marker_group)
                self.master.add_fixed_width(new_object.marker, base_width=base_r)

        return new_object

    def add_basic_label(self, name, local_x, local_y, text, font_size= 12, colour= (0,0,0,255)):
        x, y = self.anim_xy(local_x, local_y)
        new_label = pyglet.text.Label(text,
                    font_name= 'Arial',
                    font_size= font_size,
                    group= self.foreground,
                    batch= self.batch,
                    color= colour,
                    x= x,
                    y= y,
                    z= 1)
        self.labels[name] = new_label
        return new_label

    def frame_update(self, dt):
        #TODO: modify for sub windows
        self.update_animation_actions()
        for id in self.fore_obj:
            self.fore_obj[id].frame_update(self.master.sim_clock, dt)
        for id in self.progress_bars:
            if self.progress_bars[id].master != None:
                self.progress_bars[id].update_attached_position()

    def find_command_from_alias(self, alias):
        assert alias in self.custom_commands or alias in self.named_commands

        if alias in self.custom_commands:
            return self.custom_commands[alias]
        else:
            return getattr(self, str(alias))

    def update_animation_actions(self):
        if self.script_index < 0:
            return
        
        if self.script_index >= len(self.script):
            return

        #t = self.current_row['time']/self.master.sim_unit_per_second
        #if t > self.master.elapsed:
        #    return
        self.sim_t = self.current_row['time']
        if self.sim_t > self.master.sim_clock:
            return
        
        #while t <= self.master.elapsed:
        while self.sim_t <= self.master.sim_clock:
            #t = self.master.elapsed - (self.master.sim_clock - sim_t)/self.master.sim_unit_per_second
            command = self.current_row['command']
            command = str.replace(command, " ", "_")
            
            assert command in self.custom_commands or command in self.named_commands

            action = None

            if command in self.custom_commands:
                action = command # Different levels of aliasing support are tricky, should really allow logging_core to do better. # = self.custom_commands[command]
            
            elif command in self.named_commands:
                action = self.named_commands[command]

            assert action is not None
            assert self.script_type is not None

            if self.script_type == "CSV":
                half_parsed = [self.current_row['arg'+str(num)] for num in range(1,7)]
                args = tuple([arg for arg in half_parsed if not pd.isna(arg)])
                kwargs = {}
            
            elif self.script_type == "JSON":
                args = self.current_row["args"]
                kwargs = self.current_row["kwargs"]

            try:
                self.find_command_from_alias(action)(*args, **kwargs)
            except AssertionError as e:
                e.add_note(f"Passed arguments were: {args} and {kwargs}")
                raise
            except KeyError as e:
                # Most commonly: the command referenced an object_id or path_id
                # that was never created (or already deleted). Re-raise with
                # script context so the user can find the offending entry.
                raise RuntimeError(
                    f"Animation command '{command}' at script index {self.script_index} "
                    f"(sim_t={self.sim_t}) referenced missing key {e}. "
                    f"Args: {args}, Kwargs: {kwargs}"
                ) from e
            
            #TODO: pbar delete...            
            #TODO: text_color
            #TODO: follow           
            
            self.script_index += 1
            if self.script_index >= len(self.script):
                self.sim_t = float('inf')
                return
            #t = self.current_row['time']/self.master.sim_unit_per_second
            self.sim_t = self.current_row['time']

    def named_point(self, name: str):
        if name in self.named_points:
            stored = self.named_points[name]
            return (stored[0], stored[1])

    @tag_command("timejump", "hyperspeed")
    def command_timejump_next(self):
        self.master.jump_to_next_event()

    @tag_command("set_clock_speed", "clock_speed", "speed")
    def command_set_clock_speed(self, speed: float):
        self.master.set_anim_speed(float(speed))

    @tag_command("name_point", "point")
    def command_name_point(self, name: str, x: float, y: float):
        self.named_points[name] = (x,y)

    @tag_command("name_points")
    def command_name_point_spread(self, base_point: str, names: int | list[str], spread_x: float, spread_y: float, offset_x: float = 0.0, offset_y: float = 0.0):
        x0, y0 = self.named_point(base_point)
        x0, y0 = (x0 + offset_x, y0 + offset_y)

        n = len(names)
        for i, name in enumerate(names):
            i = i+1
            frac = i/n
            self.named_points[name] = (x0 + spread_x * frac, y0 + spread_y * frac)

    @tag_command("add_object", "add")
    def command_add_object(self, object_id: str, image_fname: str, scale: float, x: float, y: float, is_background: bool):
        # x, y = self.anim_xy(float(x), float(y)) 
        self.add_object(id = object_id, image_fname= image_fname, scale = float(scale), x0 = float(x), y0 = float(y), is_background= bool(int(is_background)))

    @tag_command("add_object_at_named_point", "add_at_point")
    def command_add_object_at_named_point(self, object_id: str, image_fname: str, scale: float, point_name: str, is_background: bool):
        self.command_add_object(object_id, image_fname, scale, *self.named_point(point_name), is_background)

    @tag_command("place_object", "place", "place_object_no_path")
    def command_place_object(self, object_id: str, x: float, y: float):
        x, y = self.anim_xy(float(x), float(y))
        self.fore_obj[object_id].place(x, y)

    @tag_command("jump_to_named_point", "place_at_named_point")
    def command_place_object_at_named_point(self, object_id: str, point_name: str, auto_orient: bool):
        self.command_place_object(object_id,*self.named_point(point_name))

    @tag_command("move", "move_no_path", "move_without_path")
    def command_move_object_without_path(self, object_id: str, x: float, y: float, t: float, auto_orient: bool):
        x, y = self.anim_xy(float(x), float(y))
        self.fore_obj[object_id].move(x1= x, y1= y, t0 = self.sim_t, t = float(t), auto_orient = bool(int(auto_orient)))

    @tag_command("move_to_named_point", "seek_named_point")
    def command_move_object_to_named_point(self, object_id: str, point_name: str, t: float, auto_orient: bool):
        self.command_move_object_without_path(object_id, *self.named_point(point_name), t, auto_orient)

    @tag_command("move_on_path", "move_on", "move_with_path")
    def command_move_object_on_path(self, object_id: str, path_id: str, t: float, auto_orient: bool, start_current: bool, end: float):
        self.fore_obj[object_id].move_on_path(path = self.paths[path_id], t0 = self.sim_t, t = float(t), auto_orient = bool(int(auto_orient)), start_current = bool(int(start_current)), end = float(end))

    @tag_command("accel_on_path", "accel_on", "accel_with_path")
    def command_accel_object_on_path(self, object_id: str, path_id: str, t: float, auto_orient: bool, start_current: bool, end: float, accel: float, v0: float):
        self.fore_obj[object_id].move_on_path(path = self.paths[path_id], t0 = self.sim_t, t = float(t), auto_orient = bool(int(auto_orient)), start_current = bool(int(start_current)), end = float(end), _accel = float(accel), _v0 = float(v0))

    @tag_command("place_on_path", "place_on")
    def command_place_object_on_path(self, object_id: str, path_id: str, auto_orient: bool, end: float):
        self.fore_obj[object_id].place_on_path(path = self.paths[path_id], auto_orient = bool(int(auto_orient)), end = float(end))

    @tag_command("set_path_true_length", "true_path_length")
    def command_set_path_true_length(self, path_id: str, true_length: float):
        self.paths[path_id].true_length = true_length

    @tag_command("set_object_true_length", "true_object_length")
    def command_set_object_true_length(self, object_id: str, true_length: float, true_scale: bool):
        self.fore_obj[object_id].set_true_length(true_length, true_scale)
    
    @tag_command("follow_leader", "follow")
    def command_follow_leader(self, object_id: str, leader_obj_id: str):
        self.fore_obj[object_id].follow_leader( self.fore_obj[leader_obj_id] )
        
    @tag_command("unfollow_leader", "unfollow")
    def command_unfollow_leader(self, object_id: str):
        self.fore_obj[object_id].unfollow_leader()

    @tag_command("attach_object", "attach", "attach_to_other","attach_to_parent")
    def command_attach_object(self, object_id: str, attach_to: str, x_offset: float, y_offset: float):
        parent = self.fore_obj[attach_to]
        self.fore_obj[object_id].attach_to(parent, float(x_offset), float(y_offset))

    @tag_command("detach_object", "detach")
    def command_detach_object(self, object_id: str):
        self.fore_obj[object_id].detach()

    @tag_command("delete_object", "delete")
    def command_delete_object(self, object_id: str):
        obj = self.fore_obj[object_id]
        obj.sprite.delete()
        if obj.marker is not None:
            self.master.remove_fixed_width(obj.marker)
            obj.marker.delete()
        del self.fore_obj[object_id]
        del self.all_obj[object_id]

    @tag_command("set_object_guide", "guide_object", "set_guide", "guide")
    def command_set_object_guide(self, object_id: str, guide_x: float, guide_y: float):
        self.fore_obj[object_id].set_guide(float(guide_x), float(guide_y))

    @tag_command("set_object_guides", "set_guides")
    def command_set_object_guides(self, object_id: str, guide_x: float, guide_y: float, guide_yr: float ):
        self.fore_obj[object_id].set_guide(float(guide_x), float(guide_y), float(guide_yr))

    @tag_command("rescale_sprite", "rescale_object_sprite", "scale")
    def command_modify_object_image_scale(self, object_id: str, rescale: float):
        self.fore_obj[object_id].sprite.scale *= float(rescale)

    @tag_command("set_visibility", "object_visibility", "visible", "set_visible")
    def command_set_object_visible(self, object_id: str, visible: bool):
        obj = self.fore_obj[object_id]
        obj.sprite.visible = bool(int(visible))
        if obj.marker is not None:
            obj.marker.visible = bool(int(visible))
    
    @tag_command("show_object")
    def command_make_visible(self, object_id: str):
        self.command_set_object_visible(object_id, True)
    
    @tag_command("hide_object")
    def command_make_invisible(self, object_id: str):
        self.command_set_object_visible(object_id, False)

    @tag_command("colour_object", "colour", "color", "recolour_object", "recolour_object_sprite")
    def command_set_object_image_colour(self, object_id: str, r: int | float | str, g: int | float | str, b: int | float | str):
        obj = self.fore_obj[object_id]
        colour = to_colour(r,g,b)
        obj.sprite.color = colour
        if obj.marker is not None:
            obj.marker.color = colour

    @tag_command("object_rotation", "rotation")
    def command_set_object_rotation(self, object_id: str, rotation: float):
        self.fore_obj[object_id].rotate(float(rotation))

    @tag_command("object_image", "image", "object_sprite")
    def command_set_object_image(self, object_id: str, image_fname: str):
        # TODO: Check with Paul if old sprite should be deleted.
        if image_fname not in self.master.images:
            self.master.images[image_fname] = pyglet.resource.image(image_fname)
        image = self.master.images[image_fname]

        self.fore_obj[object_id].sprite.image = image

    # TODO: Improve argument order?
    @tag_command("create_label", "text_field")
    def command_add_text_field(self, new_id: str, text: str, font: str, size: int, x: int, y: int):
        x,y = self.anim_xy(int(x),int(y))
        self.labels[new_id] = pyglet.text.Label(text, font_name = font, font_size=int(size), batch = self.batch, color = (0,0,0,255), x=x, y=y, z=1)

    @tag_command("change_label", "label_text", "text")
    def command_change_text(self, id: str, text: str):
        self.labels[id].text = text

    @tag_command("label_colour", "colour_label", "color_label", "text_colour", "text_color")
    def command_colour_text(self, id, r: int | float | str, g: int | float | str, b: int | float | str):
        self.labels[id].color = to_colour(r,g,b)

    @tag_command("add_pbar", "add_progress_bar")
    def command_add_progress_bar(self, id: str, local_x: float, local_y: float, width: float, height: float, rotation: float = 0.0):
        x, y = self.anim_xy(float(local_x), float(local_y))
        width = float(width)
        height = float(height)
        rotation = float(rotation)

        new_pbar = DesVizProgressBar(id, x, y, width, height, rotation, colour=(255,0,255,255), batch = self.batch, group = self.foreground)
        self.progress_bars[id] = new_pbar

    @tag_command("del_pbar", "delete_progress_bar")
    def command_del_progress_bar(self, id):
        del self.progress_bars[id]

    @tag_command("pbar_level", "update_progress_bar", "progress_bar_level")
    def command_update_progress_bar_level(self, bar: str, level: float):
        pbar = self.progress_bars[bar]
        pbar.update_level(float(level))

    @tag_command("pbar_attach", "attach_progress_bar")
    def command_attach_progress_bar(self, bar: str, attach_to: str, x_offset: float = 0.0, y_offset: float = 0.0, rotation: float = 0.0): # Check unit of rotation
        pbar = self.progress_bars[bar]
        pbar.attach_to_object(obj = self.fore_obj[attach_to], x_offset = x_offset, y_offset = y_offset, rotation = rotation)

    @tag_command("pbar_colour", "pbar_color", "colour_progress_bar")
    def command_colour_progress_bar(self, bar, r: int | float | str, g: int | float | str, b: int | float | str):
        pbar = self.progress_bars[bar]
        color = to_colour(r,g,b)
        pbar.colour = (color[0], color[1], color[2], 255)
        pbar.bar.color = (color[0], color[1], color[2], 255)
        pass

    @tag_command("custom")
    def command_custom(self, custom_command_name, *args, **kwargs):
        if custom_command_name not in self.custom_commands:
            raise KeyError("Custom command not found.")
        
        self.find_command_from_alias(custom_command_name)(*args, **kwargs)

    def hit_test(self, x, y):
        return (self.x0 < x < self.x0 + self.Wx and self.y0 < y < self.y0 + self.Hy)

class TextWidget:
    def __init__(self, text, x, y, width, batch, group):
        self.document = pyglet.text.document.UnformattedDocument(text)
        self.document.set_style(0, len(self.document.text), dict(color=(0, 0, 0, 255), font_name='Arial'))
        font = self.document.get_font()
        height = font.ascent - font.descent

        self.layout = pyglet.text.layout.IncrementalTextLayout(self.document, width, height, batch=batch, group=group)
        self.layout.position = x, y, 0
        self.caret = pyglet.text.caret.Caret(self.layout)
        # Rectangular outline
        pad = 2
        self.rectangle = pyglet.shapes.Rectangle(x - pad, y - pad, width + pad, height + pad, (255, 255, 255), batch, group= group)

    def hit_test(self, x, y):
        return (0 < x - self.layout.x < self.layout.width and
                0 < y - self.layout.y < self.layout.height)


#NOTE: some code here (particularly event handlers) is copied and adapted from the pyglet example text_input.py
#
class CtrlWindow (SubWindow):
    """The simulation-clock + speed-control overlay.

    Rendered on its own batch (overlay_batch) so it is unaffected by
    DesVizMaster's camera transform — pan and zoom move everything except
    this. Re-docks itself on window resize. Hosts the sim_clock display,
    speed up/down buttons, and the skip-forward text input.
    """
    def __init__(self, master: "DesVizMaster", x0, y0, Wx, Hy, decimals =0):
        super().__init__(master= master, x0= x0, y0= y0, Wx= Wx, Hy= Hy,
                                   x0_ref= 0, y0_ref= 0, x_scale= 1, y_scale= 1, render_layer= 9, name= 'ctrl', pixel_units=True)
        # Use overlay batch so ctrl window is not affected by camera pan/zoom
        self.batch = master.overlay_batch
        self.main = master
        self.decimals = decimals

        clock_y = Hy - (10 + 12)
        self.clock_title = self.add_label(10, clock_y, 'Simulation clock:')
        self.clock_label = self.add_label(150, clock_y, '0')
        speedo_y = clock_y - 30
        self.speed_label = self.add_label(10, speedo_y, 'Animation speed:')
        x, y = self.anim_xy(150, speedo_y-4)
        self.speed_ctrl = TextWidget('1', x, y, 90, self.batch, self.foreground)
        skip_y = speedo_y - 30
        self.skip_label = self.add_label(10, skip_y, 'Skip forward to:')
        x, y = self.anim_xy(150, skip_y-4)
        self.skip_ctrl = TextWidget('0', x, y, 90, self.batch, self.foreground)

        self.text_cursor = master.get_system_mouse_cursor('text')
        self.widgets = [self.speed_ctrl, self.skip_ctrl]

        x, y = self.anim_xy(0, skip_y - 14)
        self.background_rect = pyglet.shapes.Rectangle(x= x, y= y, width= Wx, height= Hy - (skip_y - 14), 
                                            color= (255,255,255,175), batch= self.batch, group= self.background)
        #print(Hy - (skip_y - 14))
        self.focus = None
    
    def add_label(self, local_x, local_y, text):
        x, y = self.anim_xy(local_x, local_y)
        return pyglet.text.Label(text,
                                font_name= 'Arial',
                                font_size= 12,
                                batch= self.batch,
                                group= self.foreground,
                                color=(0,0,0,255),
                                x= x,
                                y= y,
                                z= 1)

    def reposition(self, new_x0, new_y0):
        dx = new_x0 - self.x0
        dy = new_y0 - self.y0
        self.x0 = new_x0
        self.y0 = new_y0
        # Shift all labels
        for label in [self.clock_title, self.clock_label, self.speed_label, self.skip_label]:
            label.x += dx
            label.y += dy
        # Shift text widgets
        for widget in self.widgets:
            widget.layout.position = (widget.layout.x + dx, widget.layout.y + dy, 0)
            widget.rectangle.x += dx
            widget.rectangle.y += dy
        # Shift background
        self.background_rect.x += dx
        self.background_rect.y += dy

    def frame_update(self, dt):
        self.clock_label.text = "{:.{}f}".format(self.master.sim_clock, self.decimals)

    def set_focus(self, focus):
        if focus is self.focus:
            return

        if self.focus:
            self.focus.caret.visible = False
            self.focus.caret.mark = self.focus.caret.position = 0

        self.focus = focus
        if self.focus:
            self.focus.caret.visible = True

    def on_mouse_motion(self, x, y, dx, dy):
        for widget in self.widgets:
            if widget.hit_test(x,y):
                self.main.set_mouse_cursor(self.text_cursor)
                break
        else:
            self.main.set_mouse_cursor(None)

    def on_mouse_press(self, x, y, button, modifiers):
        for widget in self.widgets:
            if widget.hit_test(x, y):
                self.main.sim_unit_per_second = 0
                self.set_focus(widget)
                break
        else:
            self.set_focus(None)

        if self.focus:
            self.focus.caret.on_mouse_press(x, y, button, modifiers)
    
    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        if self.focus:
            self.focus.caret.on_mouse_drag(x, y, dx, dy, buttons, modifiers)

    def on_text(self, text):
        if self.focus:
            self.focus.caret.on_text(text)

    def on_text_motion(self, motion):
        if self.focus:
            self.focus.caret.on_text_motion(motion)

    def on_text_motion_select(self, motion):
        if self.focus:
            self.focus.caret.on_text_motion_select(motion)

    def on_key_press(self, symbol, modifiers):
        if symbol == pyglet.window.key.TAB:
            if modifiers & pyglet.window.key.MOD_SHIFT:
                direction = -1
            else:
                direction = 1

            if self.focus in self.widgets:
                i = self.widgets.index(self.focus)
            else:
                i = 0
                direction = 0

            self.set_focus(self.widgets[(i + direction) % len(self.widgets)])
        elif symbol == pyglet.window.key.ENTER:
            if self.focus == self.speed_ctrl:
                user_str = self.speed_ctrl.document.text
                if is_float(user_str):
                    t = float(user_str)
                    if t >= 0:
                        self.master.set_anim_speed(float(user_str))
                    else:
                        self.speed_ctrl.document.text = str(self.master.sim_unit_per_second)
                else:
                    self.speed_ctrl.document.text = str(self.master.sim_unit_per_second)
            elif self.focus == self.skip_ctrl:
                user_str = self.skip_ctrl.document.text
                if is_float(user_str):
                    t = float(user_str)
                    if t >= 0:
                        self.master.skip_to_time(t)
                    else:
                        self.skip_ctrl.document.text = '0'
                else:
                    self.skip_ctrl.document.text = '0'
            self.set_focus(None)


class DesVizMaster(pyglet.window.Window):
    """Top-level pyglet window. Owns the rendering batch and subwindow stack.

    A DesVizMaster is a single OS-level window that may contain up to ten
    SubWindows stacked as render layers (smaller order = drawn first).
    Layer 9 is conventionally the CtrlWindow overlay (sim clock, speed
    control, help bar) — drawn last and unaffected by the camera transform.

    Manages the simulation playback clock (sim_clock advanced via
    frame_update at the configured anim_speed), pan/zoom (mouse scroll +
    middle-drag, with Home to reset), and the fixed-width shape system
    that counter-scales track lines and markers on zoom so they don't
    fatten with the camera.

    run(dt) starts the pyglet event loop with the given frame interval.
    """
    def __init__(self, a_window_width, a_window_height, true_scale_mode='none'):
        super().__init__(a_window_width, a_window_height, resizable=True)
        self.batch = pyglet.graphics.Batch()
        self.images = {}
        self.sim_unit_per_second = 1
        self.sim_clock = 0      #keeps track of the simulation clock
        self.elapsed = 0
        self.show_coords = False
        self.coords_label = None
        self.true_scale_mode = true_scale_mode  # 'none', 'y', or 'xy'

        self.custom_frame_updates = []

        # Position markers
        self.show_markers = False
        self.marker_radius = 5
        self.marker_colour = (0, 0, 0, 255)
        # Opt-in: colour each marker by its object's sprite (average opaque
        # pixel colour) instead of the global marker_colour. Off by default so
        # existing behaviour is unchanged.
        self.marker_from_sprite = False
        self._sprite_avg_colour = {}   # image_fname -> (r,g,b,a) cache

        # Camera pan/zoom state
        self.cam_x = 0.0
        self.cam_y = 0.0
        self.cam_zoom = 1.0
        self._identity_view = pyglet.math.Mat4()
        self._is_panning = False
        self._prev_cam_zoom = 1.0
        self._fixed_width_shapes = []  # list of (shape, base_width) for counter-scaling on zoom
        self.overlay_batch = pyglet.graphics.Batch()  # for ctrl overlay (not affected by camera)

        #create the main sub-window
        self.subwindow = SubWindow(master= self, x0= 0, y0= 0, Wx= a_window_width, Hy= a_window_height,
                                   x0_ref= 0, y0_ref= 0, x_scale= 1, y_scale= 1, render_layer= 0)
        self.ctrl_window = None
        self.subwindows = []               #max of 10 subwindow render layers (0 bottom layer, 9 top layer)
        self.subwindows_dict: dict[str, SubWindow] = {}
        for _ in range(10):
            self.subwindows.append([])

        self.subwindows[0].append(self.subwindow)
        self._help_bar_height = 0
        self._help_bar_elements = []
        self._on_reset_handlers = []

    def dock_help_bar(self, text='Scroll: zoom | Drag: pan | \u2190/\u2192: speed | Home: reset view | V: snapshot view', height=22):
        """Add a help bar at the bottom of the window on the overlay batch."""
        self._help_bar_height = height
        overlay_bg = pyglet.graphics.Group(order=18)
        overlay_fg = pyglet.graphics.Group(order=19)
        self._help_bar_bg = pyglet.shapes.Rectangle(
            x=0, y=0, width=self.width, height=height,
            color=(255, 255, 255, 255), batch=self.overlay_batch, group=overlay_bg)
        self._help_bar_label = pyglet.text.Label(
            text, font_name='Arial', font_size=10,
            color=(80, 80, 80, 255), x=8, y=5,
            batch=self.overlay_batch, group=overlay_fg)
        self._help_bar_status_label = pyglet.text.Label(
            '', font_name='Arial', font_size=10,
            color=(80, 80, 80, 255), x=self.width - 8, y=5, anchor_x='right',
            batch=self.overlay_batch, group=overlay_fg)
        self._help_bar_elements = [self._help_bar_bg, self._help_bar_label, self._help_bar_status_label]
        self._last_mouse_x = None
        self._last_mouse_y = None
        self._update_help_status()

    def _update_help_status(self, mouse_x=None, mouse_y=None):
        """Refresh the right-aligned help-bar readout (cam state + cursor in external coords)."""
        if not getattr(self, '_help_bar_status_label', None):
            return
        if mouse_x is not None:
            self._last_mouse_x = mouse_x
            self._last_mouse_y = mouse_y

        cam_str = f"cam: ({self.cam_x:.0f}, {self.cam_y:.0f}) z={self.cam_zoom:.3f}  size: {self.width}×{self.height}"

        mx, my = self._last_mouse_x, self._last_mouse_y
        if mx is None:
            self._help_bar_status_label.text = cam_str
            return

        pre_x = (mx - self.cam_x) / self.cam_zoom
        pre_y = (my - self.cam_y) / self.cam_zoom
        ext_x = ext_y = None
        for layer in reversed(self.subwindows):
            for sw in layer:
                if sw.pixel_units:
                    continue
                if sw.x0 <= pre_x <= sw.x0 + sw.Wx and sw.y0 <= pre_y <= sw.y0 + sw.Hy:
                    ext_x = sw.x0_ref + (pre_x - sw.x0) / sw.x_scale
                    ext_y = sw.y0_ref + (pre_y - sw.y0) / sw.y_scale
                    break
            if ext_x is not None:
                break

        if ext_x is not None:
            cur_str = f"  cur: ({ext_x:.0f}, {ext_y:.0f})"
        else:
            cur_str = f"  cur: ({pre_x:.0f}, {pre_y:.0f})px"
        self._help_bar_status_label.text = cam_str + cur_str

    def snapshot_view(self, path='view_snapshot.json'):
        """Write current pan/zoom state and window size to JSON and echo to stdout."""
        snapshot = {
            'cam_x': self.cam_x,
            'cam_y': self.cam_y,
            'cam_zoom': self.cam_zoom,
            'width': self.width,
            'height': self.height,
        }
        with open(path, 'w') as f:
            json.dump(snapshot, f, indent=2)
        print(f"View snapshot \u2192 {path}: cam=({self.cam_x:.2f}, {self.cam_y:.2f}) "
              f"zoom={self.cam_zoom:.4f} size={self.width}\u00d7{self.height}")

    def set_ctrl_window(self, x0 = 0, y0 = 0, Wx= 250, Hy= 96):
        self.ctrl_window = CtrlWindow(master= self, x0= x0, y0= y0, Wx= 250, Hy= 96)
        self.subwindows[9].append(self.ctrl_window)

    def dock_ctrl_window(self, dock_pos= 'TL', decimals= 0):
        self._ctrl_dock_pos = dock_pos
        self._ctrl_decimals = decimals
        w = 250
        h = 96
        if dock_pos=='TL':
            self.ctrl_window = CtrlWindow(master= self, x0= 0, y0= self.height - h, Wx= w, Hy= h, decimals= decimals)
        elif dock_pos == 'TR':
            self.ctrl_window = CtrlWindow(master= self, x0= self.width - w, y0= self.height - h, Wx= w, Hy= h, decimals= decimals)
        elif dock_pos == 'BL':
            self.ctrl_window = CtrlWindow(master= self, x0= 0, y0= 0, Wx= w, Hy= h, decimals= decimals)
        else: #BR
            self.ctrl_window = CtrlWindow(master= self, x0= self.width - w, y0= 0, Wx= w, Hy= h, decimals= decimals)
        self.subwindows[9].append(self.ctrl_window)

    def sprite_avg_colour(self, fname, image):
        """Average colour of an image's opaque pixels, cached by fname. Used to
        tint markers to match their sprite when marker_from_sprite is on."""
        if fname in self._sprite_avg_colour:
            return self._sprite_avg_colour[fname]
        try:
            raw = image.get_image_data().get_data('RGBA', image.width * 4)
            px = memoryview(raw)
            n = image.width * image.height
            sr = sg = sb = cnt = 0
            for i in range(n):
                a = px[i * 4 + 3]
                if a > 16:
                    sr += px[i * 4]; sg += px[i * 4 + 1]; sb += px[i * 4 + 2]; cnt += 1
            col = ((sr // cnt, sg // cnt, sb // cnt, 255) if cnt else self.marker_colour)
        except Exception:
            col = self.marker_colour
        self._sprite_avg_colour[fname] = col
        return col

    def add_subwindow(self, script_fname, x0, y0, Wx, Hy, *, x_scale= 1, y_scale= 1, x0_ref = None, y0_ref = None, render_layer= 1, name: str = '', pixel_units: bool = False):
        new_sw = SubWindow(self, x0, y0, Wx, Hy, x0_ref = x0_ref, y0_ref = y0_ref, x_scale = x_scale, y_scale = y_scale, render_layer = render_layer, name = name, pixel_units = pixel_units)
        self.subwindows[render_layer].append(new_sw)
        new_sw.set_script(script_fname)
        if name != '':
            self.subwindows_dict[name] = new_sw
        return new_sw
    
    def find_subwindow(self, name) -> SubWindow | None:
        return self.subwindows_dict.get(name, None)

    def jump_to_next_event(self):
        t_next = self.subwindow.next_event_time_after(self.sim_clock)
        for layer in self.subwindows:
            for sw in layer:
                if t_next is None:
                    t_next = sw.next_event_time_after(self.sim_clock)
                
                t_new = sw.next_event_time_after(self.sim_clock)
                if t_new is not None:
                    t_next = min(t_next, t_new)

        if t_next is not None:
            self.set_anim_speed(t_next - self.sim_clock)

    def add_fixed_width(self, shape, base_width=None, attr=None):
        """Register a shape whose size should stay constant on screen regardless of zoom.
        attr: the attribute to scale ('width', 'radius', etc). Auto-detected if None."""
        if attr is None:
            attr = 'radius' if hasattr(shape, 'radius') else 'width'
        if base_width is None:
            base_width = getattr(shape, attr, None) or getattr(shape, '_' + attr, 1)
        self._fixed_width_shapes.append((shape, base_width, attr))

    def remove_fixed_width(self, shape):
        """Unregister a shape from fixed-width scaling."""
        self._fixed_width_shapes = [(s, bw, a) for s, bw, a in self._fixed_width_shapes if s is not shape]

    def skip_to_time(self, target_t):
        """Skip to a target time, replaying from the start if needed."""
        if target_t < self.sim_clock:
            # Going backwards — reset and replay
            for layer in self.subwindows:
                for sw in layer:
                    if sw.script is None:
                        continue
                    # Delete all foreground objects and their markers
                    for obj_id in list(sw.fore_obj.keys()):
                        obj = sw.fore_obj[obj_id]
                        obj.sprite.delete()
                        if obj.marker is not None:
                            self.remove_fixed_width(obj.marker)
                            obj.marker.delete()
                    sw.fore_obj.clear()
                    sw.all_obj.clear()
                    # Reset script to beginning
                    sw.script_index = 0
                    sw.sim_t = 0
            # Notify custom reset handlers (e.g. lock_lines)
            for fn in self._on_reset_handlers:
                fn()
            self.sim_clock = 0
            self.elapsed = 0
        # Fast-forward to target
        self.sim_clock = target_t

    def add_on_reset(self, handler):
        """Register a callback to be called when the animation resets for time skip backwards."""
        if not hasattr(self, '_on_reset_handlers'):
            self._on_reset_handlers = []
        self._on_reset_handlers.append(handler)

    def set_anim_speed(self, sim_unit_per_second):
        if self.ctrl_window != None:
            self.ctrl_window.speed_ctrl.document.text = '%.6g' % sim_unit_per_second
        self.sim_unit_per_second = sim_unit_per_second
    
    def set_script(self, script_fname):
        self.subwindow.set_script(script_fname)
    
    def set_paths(self, path_fname):
        self.subwindow.set_paths(path_fname)

    def set_show_coords(self, x= 2, y= 4):
        if self.ctrl_window == None:
            self.dock_ctrl_window()
        self.show_coords = True
        self.coords_label = pyglet.text.Label('(0,0)',
                                font_name= 'Arial',
                                font_size= 12,
                                batch= self.batch,
                                group= self.ctrl_window.foreground,
                                color=(0,0,0,255),
                                x= x,
                                y= y,
                                z= 1)

    def frame_update(self, dt):
        for i in range(len(self.subwindows)):
            for sw in self.subwindows[i]:
                sw.frame_update(dt * self.sim_unit_per_second)
        for fn in self.custom_frame_updates:
            fn(dt * self.sim_unit_per_second)
        self.elapsed += dt
        self.sim_clock += dt * self.sim_unit_per_second
    
    def flush_resize(self, timeout=1.0):
        """Pump OS events until the GL back buffer size changes from its current
        value, or timeout. Use after set_size() in non-event-loop contexts: pyglet
        updates self.width synchronously but the OS window resize is async via
        WM_SIZE; without pumping events the framebuffer stays at construction size.
        """
        self.switch_to()
        initial = pyglet.image.get_buffer_manager().get_color_buffer()
        initial_size = (initial.width, initial.height)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.dispatch_events()
            cur = pyglet.image.get_buffer_manager().get_color_buffer()
            if (cur.width, cur.height) != initial_size:
                return
            time.sleep(0.02)

    def run(self, frame_interval):
        pyglet.clock.schedule_interval(self.frame_update, frame_interval)
        pyglet.app.run()

    def run_export(self, out_dir, start_sim, end_sim, speed, fps,
                   view_snapshot=None, make_mp4=True, hide_help_bar=True,
                   hide_ctrl=False, run_in_sim=0.0):
        """Render a fixed-step clip to a PNG sequence and (optionally) an MP4.

        Drives sim_clock deterministically rather than relying on
        pyglet.clock; each frame is rendered at sim time
        start_sim + i * (end_sim - start_sim) / num_frames, where
        num_frames = round((end_sim - start_sim) / speed * fps).

        run_in_sim: if > 0, simulate this many sim-seconds before start_sim
        without writing PNGs. Lets per-frame state (follower path queues,
        true-scale, etc.) settle naturally rather than being established
        by a single zero-dt frame_update after a time skip.

        Writes frame_NNNNN.png into out_dir, plus snippet.tex with a
        ready-to-paste \\animategraphics line. If ffmpeg is on PATH and
        make_mp4 is True, also writes preview.mp4.
        """
        if end_sim <= start_sim:
            raise ValueError(f"end_sim ({end_sim}) must be > start_sim ({start_sim})")
        if speed <= 0:
            raise ValueError(f"speed must be positive, got {speed}")
        if run_in_sim < 0:
            raise ValueError(f"run_in_sim must be >= 0, got {run_in_sim}")
        if start_sim - run_in_sim < 0:
            raise ValueError(f"start_sim - run_in_sim < 0 (would skip before sim start)")

        os.makedirs(out_dir, exist_ok=True)

        if view_snapshot:
            with open(view_snapshot) as f:
                v = json.load(f)
            self.cam_x = v['cam_x']
            self.cam_y = v['cam_y']
            self.cam_zoom = v['cam_zoom']
            snap_w, snap_h = v.get('width'), v.get('height')
            if snap_w is not None and snap_h is not None:
                if (snap_w, snap_h) != (self.width, self.height):
                    print(f"WARNING: snapshot recorded size {snap_w}×{snap_h} "
                          f"but current window is {self.width}×{self.height}. "
                          f"Frames will be rendered at the current size. "
                          f"Resize the window to match before exporting if you want the snapshot's framing.")

        if hide_help_bar:
            for el in self._help_bar_elements:
                if hasattr(el, 'visible'):
                    el.visible = False

        if hide_ctrl and getattr(self, 'ctrl_window', None) is not None:
            cw = self.ctrl_window
            for lbl in (cw.clock_title, cw.clock_label, cw.speed_label, cw.skip_label):
                try:
                    lbl.delete()
                except Exception:
                    pass
            for wdg in getattr(cw, 'widgets', []):
                for part in ('layout', 'caret', 'rectangle'):
                    obj = getattr(wdg, part, None)
                    if obj is not None:
                        try:
                            obj.delete()
                        except Exception:
                            pass
            try:
                cw.background_rect.delete()
            except Exception:
                pass
            self.subwindows[9] = [s for s in self.subwindows[9] if s is not cw]
            self.ctrl_window = None

        self.set_anim_speed(speed)

        duration_real = (end_sim - start_sim) / speed
        num_frames = max(1, int(round(duration_real * fps)))
        sim_dt_per_frame = (end_sim - start_sim) / num_frames

        # Skip to (start - run_in); zero-dt frame_update flushes script events up to that point.
        warmup_start = start_sim - run_in_sim
        self.skip_to_time(warmup_start)
        self.frame_update(0)

        # Warmup: run frame_update incrementally from warmup_start up to start_sim
        # without saving PNGs, so per-frame state settles naturally.
        if run_in_sim > 0:
            n_warmup = max(1, int(round(run_in_sim / sim_dt_per_frame)))
            warmup_dt_sim = run_in_sim / n_warmup
            print(f"Warming up sim {warmup_start} → {start_sim} ({n_warmup} frames, no PNG)...")
            for _ in range(n_warmup):
                self.frame_update(warmup_dt_sim / speed)

        print(f"Exporting {num_frames} frames @ {fps} fps "
              f"(sim {start_sim} → {end_sim}, speed {speed} sim/sec, "
              f"clip duration {duration_real:.2f} s)")

        self.switch_to()
        self.activate()  # ensure window has focus / is composited
        # Pump any pending events (window-show, resize, etc.) before first frame.
        pyglet.app.platform_event_loop.dispatch_posted_events()
        for i in range(num_frames):
            if i > 0:
                self.frame_update(sim_dt_per_frame / speed)
            self.switch_to()
            self.on_draw()
            glFinish()  # block until all GL commands complete before reading buffer
            png_path = os.path.join(out_dir, f"frame_{i}.png")
            buf = pyglet.image.get_buffer_manager().get_color_buffer()
            buf.save(png_path)
            if i == 0 and (buf.width, buf.height) != (self.width, self.height):
                print(f"  PNG resolution: {buf.width}×{buf.height} "
                      f"(window is {self.width}×{self.height} — framebuffer is HiDPI-scaled).")
            self.flip()
            if (i + 1) % 50 == 0 or i == num_frames - 1:
                print(f"  frame {i+1}/{num_frames}")

        basename = os.path.basename(os.path.normpath(out_dir)) or 'clip'
        snippet_path = os.path.join(out_dir, 'snippet.tex')
        with open(snippet_path, 'w') as f:
            f.write(
                f"\\animategraphics[autoplay,loop,width=\\linewidth]"
                f"{{{fps}}}{{{basename}/frame_}}{{0}}{{{num_frames - 1}}}\n"
            )
        print(f"Beamer snippet → {snippet_path}")

        if make_mp4:
            ffmpeg = shutil.which('ffmpeg')
            if ffmpeg is None:
                print("ffmpeg not found on PATH; skipping preview.mp4 (PNG sequence is ready).")
            else:
                mp4_path = os.path.join(out_dir, 'preview.mp4')
                cmd = [
                    ffmpeg, '-y', '-loglevel', 'error',
                    '-framerate', str(fps),
                    '-start_number', '0',
                    '-i', os.path.join(out_dir, 'frame_%d.png'),
                    '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18',
                    mp4_path,
                ]
                print("Encoding preview.mp4...")
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    print(f"Preview → {mp4_path}")
                else:
                    print(f"ffmpeg failed (return code {result.returncode}):\n{result.stderr}")

    ##event handlers

    def on_resize(self, width, height):
        super().on_resize(width, height)
        if self.ctrl_window is not None:
            w, h = self.ctrl_window.Wx, self.ctrl_window.Hy
            dock = getattr(self, '_ctrl_dock_pos', 'TL')
            if dock == 'TL':
                new_x0, new_y0 = 0, height - h
            elif dock == 'TR':
                new_x0, new_y0 = width - w, height - h
            elif dock == 'BL':
                new_x0, new_y0 = 0, 0
            else:  # BR
                new_x0, new_y0 = width - w, 0
            self.ctrl_window.reposition(new_x0, new_y0)
        if self._help_bar_elements:
            self._help_bar_bg.width = width
            if getattr(self, '_help_bar_status_label', None):
                self._help_bar_status_label.x = width - 8

    def on_draw(self):
        self.clear()
        # Counter-scale fixed-width shapes when zoom changes
        if self.cam_zoom != self._prev_cam_zoom:
            for shape, base_val, attr in self._fixed_width_shapes:
                new_val = base_val / self.cam_zoom
                if hasattr(shape, attr):
                    setattr(shape, attr, new_val)
                else:
                    setattr(shape, '_' + attr, new_val)
                    shape._update_vertices()
            self._prev_cam_zoom = self.cam_zoom
        # Apply camera pan/zoom to main content
        self.view = self._identity_view.translate((self.cam_x, self.cam_y, 0)).scale((self.cam_zoom, self.cam_zoom, 1))
        self.batch.draw()
        # Draw overlay (ctrl window) without camera transform
        self.view = self._identity_view
        self.overlay_batch.draw()
    
    def on_mouse_motion(self, x, y, dx, dy):
        self._update_help_status(x, y)
        if self.ctrl_window != None:
            self.ctrl_window.on_mouse_motion(x, y, dx, dy)
    
    def on_mouse_press(self, x, y, button, modifiers):
        if button == pyglet.window.mouse.MIDDLE or button == pyglet.window.mouse.LEFT:
            # Don't pan if clicking on ctrl window
            if self.ctrl_window and self.ctrl_window.hit_test(x, y):
                pass
            else:
                self._is_panning = True
                if button == pyglet.window.mouse.LEFT:
                    return
        if self.show_coords:
            local_x = 0
            local_y = 0
            sw_name = ''
            for i in range(len(self.subwindows)):
                for sw in self.subwindows[i]:
                    if sw.hit_test(x,y):
                        sw_name = sw.name
                        local_x = (x - sw.x0)/sw.x_scale
                        local_y = (y - sw.y0)/sw.y_scale
            self.coords_label.text = sw_name + ': (' + str(local_x) + ',' + str(local_y) + ')'
        if self.ctrl_window != None:
            self.ctrl_window.on_mouse_press(x, y, button, modifiers)

    def on_mouse_release(self, x, y, button, modifiers):
        if button == pyglet.window.mouse.MIDDLE or button == pyglet.window.mouse.LEFT:
            self._is_panning = False

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        if self._is_panning and (buttons & pyglet.window.mouse.MIDDLE or buttons & pyglet.window.mouse.LEFT):
            self.cam_x += dx
            self.cam_y += dy
            self._update_help_status(x, y)
            return
        if self.ctrl_window != None:
            self.ctrl_window.on_mouse_drag(x, y, dx, dy, buttons, modifiers)

    def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        old_zoom = self.cam_zoom
        self.cam_zoom *= 1.1 ** scroll_y
        self.cam_zoom = max(0.01, self.cam_zoom)
        # Keep the point under the mouse fixed
        self.cam_x = x - (x - self.cam_x) * (self.cam_zoom / old_zoom)
        self.cam_y = y - (y - self.cam_y) * (self.cam_zoom / old_zoom)
        self._update_help_status(x, y)

    def on_text(self, text):
        if self.ctrl_window != None:
            self.ctrl_window.on_text(text)

    def on_text_motion(self, motion):
        if self.ctrl_window != None:
            self.ctrl_window.on_text_motion(motion)

    def on_text_motion_select(self, motion):
        if self.ctrl_window != None:
            self.ctrl_window.on_text_motion_select(motion)

    def on_key_press(self, symbol, modifiers):
        if symbol == pyglet.window.key.ESCAPE:
            pyglet.app.exit()
        elif symbol == pyglet.window.key.HOME:
            self.cam_x = 0.0
            self.cam_y = 0.0
            self.cam_zoom = 1.0
            self._update_help_status()
        elif symbol == pyglet.window.key.RIGHT:
            self.set_anim_speed(self.sim_unit_per_second * 1.5)
        elif symbol == pyglet.window.key.LEFT:
            self.set_anim_speed(max(0.001, self.sim_unit_per_second / 1.5))
        elif symbol == pyglet.window.key.V and (self.ctrl_window is None or self.ctrl_window.focus is None):
            self.snapshot_view()
        elif self.ctrl_window != None:
            self.ctrl_window.on_key_press(symbol, modifiers)