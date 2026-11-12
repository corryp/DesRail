#pragma once

#include <string>
#include <map>
#include <random>
#include <cassert>
#include <functional>
#include <algorithm>
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
	int colour_r = 255;	//sprite colour (RGB)
	int colour_g = 255;
	int colour_b = 255;

	RollingStockTemplate(string _name, double _length) :
		name(_name), length(_length) {}

	RollingStockTemplate(string _name, double _length,
		string _spritef, double _sprite_w, double _sprite_h, double _sprite_scale,
		int _colour_r = 255, int _colour_g = 255, int _colour_b = 255) :

		name(_name), length(_length),
		spritef(_spritef), sprite_w(_sprite_w), sprite_h(_sprite_h), sprite_scale(_sprite_scale),
		colour_r(_colour_r), colour_g(_colour_g), colour_b(_colour_b) {}
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
class ClosedLoopTrainSpawner;
class JobSpawnerBase;

// Top-level driver for a simulation run. Constructor reads every CSV in
// the input directory, builds the RailNetwork, and prepares the Sim. run()
// dispatches to the appropriate experiment mode (single replicate,
// throughput, deadlock-avoidance) and returns the summary log.
//
// reset() recreates per-iteration state (Sim, SignalsManager, spawners)
// without re-reading CSVs — used by the deadlock-avoidance learner to
// re-run with a different constraint set cheaply. The cached_*_csv fields
// hold the parsed CSV data so reset doesn't touch disk.
class RailSimMaster {
public:
	Sim sim;
	map<string, RollingStockTemplate*> rs_templates;
	map<string, TrainTemplate*> train_templates;
	RailNetwork* network;
	bool delete_network;	//flag to delete network in dtor
	vector<OpenLoopTrainSpawner*> open_loop_spawners;
	// Closed-loop dispatch (persistent trains chaining jobs). Coexists with the
	// open-loop spawners above; the manager owns the persistent fleet only.
	TrainManagerBase* manager = nullptr;
	vector<ClosedLoopTrainSpawner*> closed_loop_spawners;
	vector<JobSpawnerBase*> job_spawners;
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
	bool pl_constraints;	//if false, passing loop signal constraints are not created
	int pl_constraint_mode;	// 0 = arc (default), 1 = seg
	double sim_run_time;	//records elapsed time of most recent simulation run
	double sim_init_time;
	double start_warmdown = 0;	// stats window END / spawn-stop fallback; 0 = unset (drain off)

	RailSimMaster(string _input_dir, string _output_dir, int seed =1, RailNetwork* _network =nullptr, bool _delete_network =true);
	~RailSimMaster();

	LogFile* run();
	void reset();	//resets sim and network for reuse without file IO

private:
	vector<TrainChart*> train_charts;

	// Cached CSV data for reset without file IO
	vector<vector<string>> cached_ols_csv;
	vector<vector<string>> cached_runctrl_csv;
	vector<vector<string>> cached_pt_csv;	//persistent_trains.csv
	vector<vector<string>> cached_js_csv;	//job_spawner.csv
	vector<vector<string>> cached_jst_csv;	//job_spawner_task.csv

	void read_passing_loops(string fname);
	void read_rs_templates(string fname);
	void read_train_templates(string fname);
	void read_open_loop_spawners(string fname);
	void create_open_loop_spawners();	//creates spawners from cached CSV data
	void read_persistent_trains(string fname);			//closed-loop: persistent train fleets
	void create_persistent_train_spawners();			//builds ClosedLoopTrainSpawners from cached CSV
	void read_job_spawners(string fname, string task_fname);	//closed-loop: job sources (parent + task file)
	void create_job_spawners();							//builds JobSpawnerBases from cached CSV
	void read_runctrl(string fname);
	void apply_runctrl();				//applies cached runctrl settings
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



// Spawns trains at a fixed point according to a configured arrival
// distribution (uniform / normal / exponential). Each spawned train gets a
// Job built from job_template — a sequence of (terminal, dwell_time) tasks.
// Trains terminate themselves on reaching the final destination; the
// spawner does not track them.
//
// As a SimObject, the spawner is itself a coroutine: run() loops sampling
// inter-arrival times and creating Trains. add_task() builds the
// job_template; called once per spawner from create_open_loop_spawners().
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

	void add_task(string terminal, bool stop, double dwell_time, double earliest_departure = -1.0);

	SimCoroutine run() override;

	static int ols_spawn_ctr;

	string distr;
	double dist_prm1;
	double dist_prm2;
	double stop_time = -1;	// stop emitting after this sim time; <0 = use master->start_warmdown (or never)
};//TrainSpawner


// Spawns a fixed fleet of PERSISTENT trains for one train_group, then stops.
// Each train is created once (persistent, wired to master->manager) and never
// despawns: after every job it re-announces via manager->ready_signal() and
// waits for the next compatible job. The fleet is specified by a spawn TERMINAL
// (the entry arc is derived from terminal->arcs[0]) -- there is no spawn-arc /
// seg+head form. spawn_count trains are emitted, one every spawn_interval (a
// fixed scalar, not a distribution); the spawn-segment-free wait serialises them.
class ClosedLoopTrainSpawner : public SimObject {
public:
	string name;					// == train_group (spawner identity)
	RailSimMaster* master;
	string train_group;
	TrainTemplate* train_template;
	Terminal* spawn_terminal;
	double spawn_interval;
	int spawn_count;

	list<Train*> trains;

	ClosedLoopTrainSpawner(string _train_group, RailSimMaster* _master,
		TrainTemplate* _train_template, Terminal* _spawn_terminal,
		double _spawn_interval, int _spawn_count);

	SimCoroutine run() override;

	static int cls_spawn_ctr;
};//ClosedLoopTrainSpawner


// Independent source of jobs for the closed-loop dispatcher. On its own
// schedule (interval_distribution over [start_time, stop_time]) it builds a Job
// from its task list -- each task a (terminal, dwell) where the dwell is sampled
// from that task's dwell_distribution -- tags it with train_group, and submits
// it to master->manager, which allocates it to a compatible ready train.
class JobSpawnerBase : public SimObject {
public:
	struct TaskSpec {
		Terminal* terminal;
		string dwell_distr;
		double dwell_prm1;
		double dwell_prm2;
	};

	string name;					// job_source_name
	RailSimMaster* master;
	string train_group;				// jobs are tagged with this group
	double start_time;
	double stop_time;				// stop emitting at/after this time; <0 = use master->start_warmdown (or never)
	string interval_distr;
	double int_prm1;
	double int_prm2;
	DistributionSampler* iat = nullptr;
	vector<TaskSpec> task_specs;
	vector<DistributionSampler*> dwell_samplers;

	JobSpawnerBase(string _name, RailSimMaster* _master, string _train_group,
		double _start_time, double _stop_time,
		string _interval_distr, double _int_prm1, double _int_prm2);
	~JobSpawnerBase();

	void add_task(Terminal* terminal, string dwell_distr, double prm1, double prm2);

	SimCoroutine run() override;
};//JobSpawnerBase


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
	// The speed-profile span the last emitted leg was built from. A leg IS one span,
	// so the sprite has to re-emit whenever the span changes -- which used to be
	// implied by the front arc changing, until a transient speed limit's clearance
	// zone made one physical arc carry two spans (Train::run's PROFILE_SPLIT).
	SpeedProfileArc* sp_seen = nullptr;
	TrackSection* section;
	double v_inst;
	double a_inst;
	double t_obs;
	queue<DirectedSegment*> release_queue;

	double back_dist_remain;	//remaining distance for back sprite to travel to follow front sprite
	double back_time_remain;	//likewise but for time

	bool finished;

	int rev_seen = 0;			//train->reversal_count as of the last consist build

	string rs_id(int i);
	bool something_changed();
	void record_state();
	void rebuild_consist();		//tear down and re-lay the consist after a reversal
};//TrainSprite

//Network configuration tools=====================

// Configuration helper that takes a passing-loop topology (the four arcs
// of the loop pattern: mainline-0, mainline-1, siding-0, siding-1, plus
// the two approach signals) and generates the standard set of signal
// constraints that prevent unsafe passing-loop crossings. Driven by
// passing_loops.csv; one PassingLoopGroup per pl_id.
//
// The auto-generated constraints encode "at most one train in each
// direction in the loop section", implemented as occupancy limits at the
// approach signals. Two constraints per loop, one per direction. A third,
// bounding the whole group at two trains, was removed 2026-10-09: it is
// implied by the other two (disjoint triples, each capped at one distinct
// train, union capped at two) and could never fire. Verified output-identical
// over 15 Toowoomba runs including five 200 h horizons. Disabled by setting runctrl.csv pl_constraints=0 (the
// signals/speeds/priorities still get configured, only the constraints
// are skipped).
class PassingLoopGroup {
public:
	PassingLoopGroup(int _id);

	int id;

	void configure(RailNetwork* network, bool create_constraints = true, int pl_constraint_mode = 0);	//once vectors below are populated, call this to configure network parameters
	void add_appr(int num, DirectedSegment* arc, double margin, double opp_speed);
	void add_mainline(int num, DirectedSegment* arc, double margin, double opp_speed, double max_length = 0.0);
	void add_siding(int num, DirectedSegment* arc, double margin, double opp_speed, double max_length = 0.0);
private:
	//all vectors here will have 2 elements
	vector<DirectedSegment*> appr;
	vector<DirectedSegment*> mainline;
	vector<DirectedSegment*> siding;

	vector<DirectedSegment*> group; //this will have all 6 elements
	vector<double> margins;		//stopping margins for signals in group
	vector<double> opp_speeds; //speed limits applied to arcs in opposite direction to those in group
	vector<double> ml_max_length;	//max_length for mainline arcs (0 = no limit), indexed by direction
	vector<double> sid_max_length;	//max_length for siding arcs (0 = no limit), indexed by direction

	SignalConstraint* sc0;
	SignalConstraint* sc1;
};//PassingLoopGroup

//Train chart tool===================================

struct ChartStart {			//one chainage datum: arc + base chainage of its tail
	DirectedSegment* arc;
	double base_chg;
};

class TrainChart {
public:
	int id;
	RailNetwork* network;
	vector<bool> exit_nodes;		//=1 if node id is an exit from the corridor (exits when node is at head of front arc - before reaching the node)
	list<ChartStart> starts;	//arcs from where chainage is calculated (tail=base_chg, head=base_chg+length); multi-source BFS takes the min, so parallel entry tracks each get their own origin
	list<DirectedSegment*> entry_arcs;	//set of arcs to monitor for entry into the corridor
	set<Train*> tracing;		//trains this corridor is currently tracing, so a consist that
								//occupies two monitored arcs at once is not traced twice

	vector<double> node_chg;		//chainage of each node
	vector<double> node_datum;	//base_chg of the start each node's chainage was measured from,
								//carried along the relaxation so check_chainage_continuity can
								//tell a deliberate datum offset from a broken corridor boundary
	double big_m;				//the 'unreached' sentinel node_chg is filled with

	LogFile tclog;

	TrainChart(int _id, RailNetwork* network, double _bigM= 1000000);
	void add_start(int seg_id, int head_id, double base_chg);
	void add_exit_node(int node_id);
	void add_entry_arc(int seg_id, int head_id);
	void initialise();
	void log_entry(Train* train);
	void check_chainage_continuity();	//see the definition: chainage must be a metric
private:
	// Heap ordering for the chainage relaxation. std::*_heap put the element the
	// comparator ranks GREATEST at the front, so comparing the other way round
	// gives a min-heap on chainage: the nearest un-finalised node pops first.
	struct NearerChainageFirst {
		const TrainChart* chart;
		bool operator()(const TrackNode* a, const TrackNode* b) const {
			return chart->node_chg[a->id] > chart->node_chg[b->id];
		}
	};

	// Relax chainage outward from the seeded nodes, shortest-path first.
	// Was a FIFO queue that froze a node's chainage the moment it was popped,
	// which is only correct when every edge has the same length; with real
	// segment lengths the value a node kept depended on VISIT ORDER, so an
	// unrelated topology change could move a chainage by hundreds of metres.
	// Ordered by chainage (Dijkstra), a node is finalised only once nothing
	// shorter remains, which is what "shortest path" was always meant to be.
	void bfs_shortest_path(vector<TrackNode*>& heap, vector<bool>& visited, vector<bool>& probed);
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
	static void flush_active();		// emit a final horizon sample for every still-active trace
	void log_now();					// log at most one sample per sim instant
private:
	bool something_changed();
	double last_log_t = -1.0;		// guards the duplicate row two same-instant changes produce
	double prev_pos;
	double prev_acc;
	DirectedSegment* prev_arc;
	bool exiting;

	static int obj_ctr;

	// Traces whose train is still in the corridor. Sampling is motion-driven, so a
	// deadlocked (permanently stalled) train stops emitting rows at its last movement;
	// flush_active() adds a final row at the horizon so its trajectory shows the flatline.
	static list<TrainChartTrace*> active;
};//TrainChartTrace



