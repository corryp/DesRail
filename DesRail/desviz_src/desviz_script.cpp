#include "desviz_script.h"
#include <map>
#include <fstream>

namespace desv {
	AnimScript::AnimScript(string _fname, Sim& _sim) : fname(_fname), sim(_sim), entry_ctr(0)
	{
		cmds["commands"] = json::array();
		cmds["version"] = "1.4.0";
		cmds["metadata"]["x0"] = 0.0;
		cmds["metadata"]["y0"] = 0.0;
		cmds["metadata"]["width"] = 1.0;
		cmds["metadata"]["height"] = 1.0;
	}

	void AnimScript::write()
	{
		ofstream f(fname);
		//cout << cmds.dump(4);
		f << cmds.dump(4);
		f.close();
	}

	void AnimScript::add_object(std::string object_id, std::string image_fname, double scale, double x, double y, bool is_background)
	{
		json kv = blank_command();
		kv["data"]["command"] = "add_object";
		kv["data"]["args"] = json::array({object_id, image_fname, scale, x, y, btoi(is_background)});
		cmds["commands"].push_back(kv);
	}

	void AnimScript::move_object_on_path(std::string object_id, std::string path_id, double t, bool auto_orient, bool start_current, double end)
	{
		json kv = blank_command();
		kv["data"]["command"] = "move_on_path";
		kv["data"]["args"] = json::array({ object_id, path_id, t, btoi(auto_orient), btoi(start_current), end });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::accel_object_on_path(std::string object_id, std::string path_id, double t, bool auto_orient, bool start_current, double end, double accel, double v0)
	{
		json kv = blank_command();
		kv["data"]["command"] = "accel_on_path";
		kv["data"]["args"] = json::array({ object_id, path_id, t, btoi(auto_orient), btoi(start_current), end, accel, v0 });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::place_object_on_path(std::string object_id, std::string path_id, bool auto_orient, double end)
	{
		json kv = blank_command();
		kv["data"]["command"] = "place_on_path";
		kv["data"]["args"] = json::array({ object_id, path_id, btoi(auto_orient), end });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::set_path_true_length(std::string path_id, double true_length)
	{
		json kv = blank_command();
		kv["data"]["command"] = "set_path_true_length";
		kv["data"]["args"] = json::array({ path_id, true_length });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::set_object_true_length(std::string object_id, double true_length, bool true_scale)
	{
		json kv = blank_command();
		kv["data"]["command"] = "set_object_true_length";
		kv["data"]["args"] = json::array({ object_id, true_length, btoi(true_scale) });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::follow_leader(std::string object_id, std::string leader_obj_id)
	{
		json kv = blank_command();
		kv["data"]["command"] = "follow_leader";
		kv["data"]["args"] = json::array({ object_id, leader_obj_id });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::unfollow_leader(std::string object_id)
	{
		json kv = blank_command();
		kv["data"]["command"] = "unfollow_leader";
		kv["data"]["args"] = json::array({ object_id });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::delete_object(std::string object_id)
	{
		json kv = blank_command();
		kv["data"]["command"] = "delete_object";
		kv["data"]["args"] = json::array({ object_id });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::set_object_guide(std::string object_id, double guide_x, double guide_y)
	{
		json kv = blank_command();
		kv["data"]["command"] = "set_object_guide";
		kv["data"]["args"] = json::array({ object_id, guide_x, guide_y });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::set_object_guides(std::string object_id, double guide_x, double guide_y, double guide_yr)
	{
		json kv = blank_command();
		kv["data"]["command"] = "set_object_guides";
		kv["data"]["args"] = json::array({ object_id, guide_x, guide_y, guide_yr });
		cmds["commands"].push_back(kv);
	}

	void AnimScript::custom(std::string custom_command_name, std::vector<std::string> args)
	{
		json kv = blank_command();
		kv["data"]["command"] = custom_command_name;
		kv["data"]["args"] = json::array();
		for (auto str : args)
			kv["data"]["args"].push_back(str);
		cmds["commands"].push_back(kv);
	}

	json AnimScript::blank_command()
	{
		json blank;
		blank["tag"] = "entry_" + to_string(entry_ctr++);
		blank["data"]["time"] = sim.time_now;
		blank["data"]["kwargs"] = json::object();
		return blank;
	}

	int AnimScript::btoi(bool b)
	{
		return b ? 1 : 0;
	}




}//desv