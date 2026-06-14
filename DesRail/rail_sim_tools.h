#pragma once

#include <string>
#include <map>
#include <random>
#include <cassert>
#include <functional>
#include "read_csv.h"
#include "desrail.h"
#include "comparison_tolerance.h"
#include "desviz_src/desviz_script.h"
#include "log_file_util.h"

using namespace std;
using namespace DesRail;
using namespace comparison_tolerance;
using namespace desv;
using namespace lfu;

const bool rst_debug = true;

class TrainSprite;
class PassingLoopGroup;
class TrainChart;
struct rgb;

struct RollingStockTemplate {
	string name;
	double length;

	//animation parameters
	string spritef;			//pathname for sprite image
	double sprite_w;		//width of sprite in pixels
	double sprite_h;		//height of sprite in pixels
	double sprite_scale;	//value between 0 and 1

	RollingStockTemplate(string _name, double _length) :
		name(_name), length(_length) {}

	RollingStockTemplate(string _name, double _length,
		string _spritef, double _sprite_w, double _sprite_h, double _sprite_scale) :
		
		name(_name), length(_length), 
		spritef(_spritef), sprite_w(_sprite_w), sprite_h(_sprite_h), sprite_scale(_sprite_scale) {}
};

struct TrainTemplate {
	string name;
	vector<RollingStockTemplate*> consist;
	double max_spd;
	double accel;
	double decel;
	double length;

	TrainTemplate(string _name, double _max_spd, double _accel, double _decel) :
		name(_name), max_spd(_max_spd), accel(_accel), decel(_decel), length(0) {}

	void append_rolling_stock(RollingStockTemplate* rs) {
		length += rs->length;
		consist.push_back(rs);
	}
};

class OpenLoopTrainSpawner;

class RailSimMaster {
public:
	Sim sim;
	map<string, RollingStockTemplate*> rs_templates;
	map<string, TrainTemplate*> train_templates;
	RailNetwork* network;
	vector<OpenLoopTrainSpawner*> open_loop_spawners;
	vector<PassingLoopGroup*> pl_groups;
	string input_dir;
	string output_dir;
	int random_seed;
	bool animate;
	AnimScript* script;
	double anim_start_t;
	double anim_end_t;
	bool render_sprites_to_scale;
	bool render_cars;
	std::mt19937 rng;
	std::mt19937 anim_rng;
	double sim_run_time;	//records elapsed time of most recent simulation run
	double sim_init_time;

	RailSimMaster(string _input_dir, string _output_dir, int seed =1);
	~RailSimMaster();
	
	LogFile* run();

private:
	vector<TrainChart*> train_charts;

	void read_passing_loops(string fname);
	void read_rs_templates(string fname);
	void read_train_templates(string fname);
	void read_open_loop_spawners(string fname);
	void read_runctrl(string fname);
	void read_train_charts(string fname);
	void initialise();
	void set_animation(string scriptf, double _anim_start_t, double _anim_end_t, bool _render_cars = true, bool _render_sprites_to_scale = false);
};//RailSimMaster

class DistributionSampler {
public:
	std::mt19937& rng;
	string distr_name;

	DistributionSampler(std::mt19937& _rng, 
		string _distr_name, double prm1, double prm2 = 0);

	double sample();

private:
	function<double()> distr;
	std::uniform_real_distribution<>* ufm;
	std::normal_distribution<>* nrm;
	std::exponential_distribution<>* exp;
	double constant;
};//DistributionSampler



//spawns trains at the specified spawn point according to interval distribution
//assigns a job to trains through specified destination
//trains are destroyed on reaching final destination
//
class OpenLoopTrainSpawner : public SimObject {
public:
	string name;
	RailSimMaster* master;
	TrainTemplate* train_template;
	DirectedSegment* spawn_arc;
	vector<Task> job_template;
	DistributionSampler *iat;		//interarrival time distribution

	list<Train*> trains;

	OpenLoopTrainSpawner(string _name, RailSimMaster* _master,
		int spawn_seg, int spawn_head,
		TrainTemplate* _train_template,
		string _distr, double dist_prm1, double dist_prm2);

	~OpenLoopTrainSpawner();

	void add_task(string terminal, bool stop, double dwell_time);

	SimCoroutine run() override;

	static int ols_spawn_ctr;

	string distr;
	double dist_prm1;
	double dist_prm2;
};//TrainSpawner


class ClosedLoopTrainSpawner : public SimObject {
public:
	string name;

	//TODO
};//ClosedLoopTrainSpawner


//Animation Tools==========================

struct rgb {
	int r;
	int g;
	int b;

	string rstr;
	string gstr;
	string bstr;

	rgb(int _r, int _g, int _b) : r(_r), g(_g), b(_b), rstr(to_string(r)), gstr(to_string(g)), bstr(to_string(b)) {}
	
	void set_rgb(int _r, int _g, int _b) {
		r = _r;
		g = _g;
		b = _b;
		rstr = to_string(r);
		gstr = to_string(g);
		bstr = to_string(b);
	}//set_rgb
};

class TrainSprite : public SimObject {
public:
	TrainSprite(RailSimMaster* _master, Train* _train, TrainTemplate* _template);

	SimCoroutine run() override;

private:
	RailSimMaster* master;
	Train* train;
	TrainTemplate* tr_template;
	rgb lock_colour;
	string obj_id;

	DirectedSegment* f_arc;
	DirectedSegment* b_arc;
	TrackSection* section;
	double v_inst;
	double a_inst;
	double t_obs;
	queue<DirectedSegment*> release_queue;

	double back_dist_remain;	//remaining distance for back sprite to travel to follow front sprite
	double back_time_remain;	//likewise but for time

	bool finished;

	string rs_id(int i);
	bool something_changed();
	void record_state();
};//TrainSprite

//Network configuration tools=====================

class PassingLoopGroup {
public:
	PassingLoopGroup(int _id);

	int id;

	void configure(RailNetwork* network);	//once vectors below are populated, call this to configure network parameters
	void add_appr(int num, DirectedSegment* arc, double margin, double opp_speed);
	void add_mainline(int num, DirectedSegment* arc, double margin, double opp_speed);
	void add_siding(int num, DirectedSegment* arc, double margin, double opp_speed);
private:
	//all vectors here will have 2 elements
	vector<DirectedSegment*> appr;
	vector<DirectedSegment*> mainline;
	vector<DirectedSegment*> siding;

	vector<DirectedSegment*> group; //this will have all 6 elements
	vector<double> margins;		//stopping margins for signals in group
	vector<double> opp_speeds; //speed limits applied to arcs in opposite direction to those in group

	SignalConstraint* sc_all;
	SignalConstraint* sc0;
	SignalConstraint* sc1;
};//PassingLoopGroup

//Train chart tool===================================

class TrainChart {
public:
	int id;
	RailNetwork* network;
	vector<bool> exit_nodes;		//=1 if node id is an exit from the corridor (exits when node is at head of front arc - before reaching the node)
	DirectedSegment* start_arc;	//arc from where the chainage is calculated
	list<DirectedSegment*> entry_arcs;	//set of arcs to monitor for entry into the corridor

	vector<double> node_chg;		//chainage of each node

	LogFile tclog;

	TrainChart(int _id, RailNetwork* network, DirectedSegment* _start_arc, double _bigM= 1000000);
	void add_exit_node(int node_id);
	void add_entry_arc(int seg_id, int head_id);
	void initialise();
	void log_entry(Train* train);
private:
	void bfs_shortest_path(list<TrackNode*>& q, vector<bool>& visited, vector<bool>& probed);
	void init_log();
};//TrainChart

class MonitorTrainChartEntry : public SimObject {
public:
	TrainChart* tc;
	DirectedSegment* arc;
	MonitorTrainChartEntry(TrainChart* _tc, DirectedSegment* _arc);
	SimCoroutine run() override;
};//MonitorTrainChartEngry

class TrainChartTrace : public SimObject {
public:
	int id;
	TrainChart* tc;
	Train* train;
	TrainChartTrace(TrainChart* _tc, Train* _train);
	SimCoroutine run() override;
private:
	bool something_changed();
	double prev_pos;
	double prev_acc;
	DirectedSegment* prev_arc;
	bool exiting;

	static int obj_ctr;

	//debugging
	static list<TrainChartTrace*> tmp;
};//TrainChartTrace



