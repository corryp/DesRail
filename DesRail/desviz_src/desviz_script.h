#pragma once

#include <string>
#include <fstream>
#include "../simulator.h"
#include "json.hpp"

using namespace std;
using json = nlohmann::json;

namespace desv {

	class AnimScript {
	public:
		string fname;
        json cmds;
        Sim& sim;

		AnimScript(string _fname, Sim& _sim);

        void write();

        //void timejump_next();
        //void set_clock_speed(double speed);
        //void name_point(std::string name, double x, double y);
        //void name_point_spread(std::string base_point, std::vector<std::string> names, double spread_x, double spread_y, double offset_x = 0.0, double offset_y = 0.0); // Assuming names is a vector of strings. You may need to adapt this based on your C++ implementation of the python type.
        void add_object(std::string object_id, std::string image_fname, double scale, double x, double y, bool is_background);
        //void place_object(std::string object_id, double x, double y);
        //void add_object_at_named_point(std::string object_id, std::string image_fname, double scale, std::string point_name, bool is_background);
        //void place_object_at_named_point(std::string object_id, std::string point_name, bool auto_orient);
        //void move_object_without_path(std::string object_id, double x, double y, double t, bool auto_orient);
        //void move_object_to_named_point(std::string object_id, std::string point_name, double t, bool auto_orient);
        void move_object_on_path(std::string object_id, std::string path_id, double t, bool auto_orient, bool start_current, double end);
        void accel_object_on_path(std::string object_id, std::string path_id, double t, bool auto_orient, bool start_current, double end, double accel, double v0);
        void place_object_on_path(std::string object_id, std::string path_id, bool auto_orient, double end);
        void set_path_true_length(std::string path_id, double true_length);
        void set_object_true_length(std::string object_id, double true_length, bool true_scale);
        void follow_leader(std::string object_id, std::string leader_obj_id);
        void unfollow_leader(std::string object_id);
        //void attach_object(std::string object_id, std::string attach_to, double x_offset, double y_offset);
        //void detach_object(std::string object_id);
        void delete_object(std::string object_id);
        void set_object_guide(std::string object_id, double guide_x, double guide_y);
        void set_object_guides(std::string object_id, double guide_x, double guide_y, double guide_yr);
        //void modify_object_image_scale(std::string object_id, double rescale);
        //void set_object_visible(std::string object_id, bool visible);
        //void make_visible(std::string object_id);
        //void make_invisible(std::string object_id);
        void set_object_image_colour(std::string object_id, int r, int g, int b); // Assuming color is represented by 3 integers. You may need to adapt this based on your C++ implementation of the python type.
        //void set_object_rotation(std::string object_id, double rotation);
        //void set_object_image(std::string object_id, std::string image_fname);
        //void add_text_field(std::string new_id, std::string text, std::string font, int size, int x, int y);
        //void change_text(std::string id, std::string text);
        //void colour_text(std::string id, int r, int g, int b); // Assuming color is represented by 3 integers. You may need to adapt this based on your C++ implementation of the python type.
        //void add_progress_bar(std::string id, double local_x, double local_y, double width, double height, double rotation = 0.0);
        //void del_progress_bar(std::string id);
        //void update_progress_bar_level(std::string bar, double level);
        //void attach_progress_bar(std::string bar, std::string attach_to, double x_offset = 0.0, double y_offset = 0.0, double rotation = 0.0);
        //void colour_progress_bar(std::string bar, int r, int g, int b); // Assuming color is represented by 3 integers. You may need to adapt this based on your C++ implementation of the python type.
        void custom(std::string custom_command_name, std::vector<std::string> args); // Adapting args to be a vector of strings. You may need to adapt this based on your C++ implementation of the python type.

    private:
        int entry_ctr;
        json blank_command();
        int btoi(bool b);
	};//SubWindow

}//desv