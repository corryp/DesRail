#pragma once
#include "desrail.h"
#include "simulator.h"
#include "desviz_src/desviz_script.h"
#include <vector>
#include <string>

using namespace DesRail;
using namespace desv;
using namespace std;

namespace dra {

	class TrainAnim;

	struct rgb {
		int r;
		int g;
		int b;

		string rstr;
		string gstr;
		string bstr;

		rgb(int _r, int _g, int _b) : r(_r), g(_g), b(_b), rstr(to_string(r)), gstr(to_string(g)), bstr(to_string(b)) {}
	};

	class RTTAnimMgr : public SimObject {
	public:
		RailNetwork* network;
		AnimScript script;
		vector<string> spritef;
		vector<string> car_spritef;
		vector<rgb> lock_colours;
		double anim_start_t;
		double anim_end_t;
		set<Train*>& active_trains;
		list<TrainAnim> train_anims;

		double train_sprite_scale;	//something between 0 and 1
		double sprite_w;			//this is to determine the guide point of the sprite (relative to its origin)
		double sprite_h;
		double car_w;
		double car_h;

		RTTAnimMgr(RailNetwork* _network, Sim& _sim, 
			string scriptf, string _spritef, string _car_spritef,
			double _sprite_scale, double _sprite_w, double _sprite_h, double _car_w, double _car_h, 
			double _anim_start_t, double _anim_end_t);

		SimCoroutine run() override;

		void add_spritef(string _spritef, string _car_spritef, int lock_r =255, int lock_g =0, int lock_b =0);
	private:
		int sprite_idx;
	};

	class TrainAnim : public SimObject {
	public:
		Train* train;
		TrainAnim(RTTAnimMgr* _mgr, Train* _train, string _spritef, string _car_spritef, rgb _lock_colour);

		SimCoroutine run() override;

	private:
		RTTAnimMgr* mgr;
		string spritef;
		string car_spritef;
		rgb lock_colour;
		string obj_id;

		DirectedSegment* f_arc;
		DirectedSegment* b_arc;
		TrackSection* section;
		double v_inst;
		double a_inst;
		double t_obs;

		double back_dist_remain;	//remaining distance for back sprite to travel to follow front sprite
		double back_time_remain;	//likewise but for time

		bool something_changed();
		void record_state();
		void calc_back_move(SpeedProfileArc* sp_arc, double& back_dist, double& back_time);
	};//train_anim
}//dra