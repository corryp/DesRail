#pragma once

#include <list>
#include <vector>
#include <cassert>
#include <string>
#include <map>
#include <set>
#include <queue>
#include "simulator.h"
#include "read_csv.h"
#include "log_file_util.h"

using namespace std;
using namespace lfu;

namespace DesRail {

	//forward declarations
	class DirectedSegment;
	class TrackNode;
	class TrackSegment;
	class TrackSection;
	class Terminal;
	class RailNetwork;
	class Train;
	class Job;
	class TrainManagerBase;
	class SignalsManager;
	class DelayedTrackRelease;
	class SignalConstraint;

	// A junction or endpoint where one or more track segments meet. Owns
	// no track itself — it is purely a connection point in the network
	// graph. Bidirectional segments contribute to both in_arcs and out_arcs.
	class TrackNode {
	public:
		int id;
		list<DirectedSegment*> in_arcs;	//inbound segments
		list<DirectedSegment*> out_arcs;	//outbound segments (not mutually exclusive for bidirectional segments)

		TrackNode(int _id);
	};//TrackNode

	// A physical track segment between two TrackNodes. State machine:
	// FREE -> LOCKED (claimed by a train, not yet entered) -> LOADED (train
	// physically present) -> FREE (after release_delay). Owns up to two
	// DirectedSegment arcs (one per direction); a unidirectional segment
	// has only the appropriate arc.
	class TrackSegment {
	public:
		int id;
		TrackNode* node1;	//tail node of the segment
		TrackNode* node2;	//head node of the segment
		double length;
		bool bidirectional;	//travel in both directions is permitted or not

		DirectedSegment* forward_arc;		//(node1,this)
		DirectedSegment* reverse_arc;		//(node2,this)
		list<TrackSection*> sections;		//track sections this segment belongs to (populated by TrackSection class)

		enum TrackState {
			FREE,		//segment is available
			LOCKED,		//segment is locked but not occupied by a train
			LOADED		//segment is occupied by at least 1 train
		};//Track state

		//dynamic variables
		TrackState state;

		Train* owner;	//TODO: could have multiple owners in future

		TrackSegment(int id);
		~TrackSegment();

		void configure(TrackNode* _node1, TrackNode* _node2, double _length, bool _bidirectional = true, bool disabled = false);
		TrackNode* other_node(TrackNode* node) const;	//returns the opposite node to that provided
		DirectedSegment* get_arc(TrackNode* head);	//returns forward_arc if head=node2, revers_arc otherwise
		DirectedSegment* get_arc(int head_id);	//as above but using node id
		void update_state(TrackState new_state, Train* _owner = nullptr);

		void reset(); //reinitialises state
	};//TrackSegment

	// An ordered sequence of arcs from one signal to the next — a single
	// signal-to-signal block, the unit of section-based access control.
	// is_available() is true when no segment in the section is locked or
	// loaded by another train. The reachable[] array is indexed by terminal
	// and used for route-aware grant decisions in SignalsManager.
	class TrackSection {
	public:
		int id;
		vector<DirectedSegment*> arcs;

		vector<bool> reachable;	//indicates reachability of terminals, indexed by their id
		Terminal* terminal;		//if section ends on terminal, points to it, otherwise null
		list<TrackSection*> upstream_sections;		//list of sections adjacent upstream (populated by RailNetwork::initialise_network)
		list<TrackSection*> downstream_sections;	//list of sections adjacent downstream (populated by RailNetwork::initialise_network)
		double length;
		double priority;

		TrackSection(int n_terminals = 2);

		void push_back(DirectedSegment* arc);
		bool is_available();		//returns true if all segments in section are FREE
		void notify_state_change(TrackSegment::TrackState old_state, TrackSegment::TrackState new_state);

		void reset(); //reinitialises state

		static int track_section_counter;
	private:
		int segments_locked_counter;			//keeps count of number of locked segments
	};//TrackSection

	struct RankSections {
		bool operator()(const TrackSection* a, const TrackSection* b) const {
			if (a->priority == b->priority)
				return a->id > b->id;
			return a->priority > b->priority;
		} // Descending order
	};
	
	// A directed arc — one orientation of a TrackSegment, identified by
	// its head node. Most rail-domain logic acts at the arc level rather
	// than the segment level: signals are placed on arcs, speed limits and
	// priorities are arc-keyed, and SignalConstraints register on arcs.
	// constraints[] is the set of all SignalConstraints currently watching
	// this arc; SignalsManager iterates these in grant_access().
	class DirectedSegment {
	public:
		int id;
		string str_id;
		TrackSegment* segment;
		TrackNode* head;
		TrackNode* tail;

		//properties
		bool is_signal;
		double margin;		//distance from signal that train will stop
		bool lock_thru;		//indicates that trains should lock next section if possible
		double priority;	//used to encourage (higher) or discourage (lower) use of the segment (cumulative within sections)

		bool is_terminal;	//indicates whether arc can be the end-point of a trip
		Terminal* terminal;	//points to the terminal this arc is part of

		bool is_speed_change;	//indicates whether there is a change in speed limit at the start of the arc
		double speed_limit;		//speed limit applied from start of arc onwards (until next speed limit encountered)

		list<DirectedSegment*> out_arcs;	//list of outbound arcs from head node (populated by RailNetwork::initialise_network)
		list<TrackSection*> in_sections;	//list of inbound sections from head node (populated by RailNetwork::initialise_network)
		list<TrackSection*> out_sections;	//list of outbound sections from head node (populated by RailNetwork::initialise_network)
		set<TrackSection*, RankSections> out_sections_ranked;	//list of outbound sections from head node in descending order of priority
		list<TrackSection*> start_sections;	//list of all sections starting from the tail node (populated by RailNetwork::initialise_network)
		list<SignalConstraint*> constraints;	//list of all signal constraints invovling this arc

		Train* owner;							//train that has locked this directional arc
		DelayedTrackRelease* pending_release;	//current pending delayed release process

		DirectedSegment(TrackSegment* _segment, TrackNode* _head);
		~DirectedSegment();
		void lock(Train* _owner);
		void release(Train* _owner);
		void delayed_release(Train* _owner);

		void reset();	//reinitialises state

		static int directed_segment_counter;
	};//DirectedSegment

	class Terminal {
	public:
		int id;
		string name;
		vector<DirectedSegment*> arcs;

		Terminal(string _name);
		void add_arc(DirectedSegment* arc);

		static int terminal_counter;
	};//Terminal

	RailNetwork* read_network(string path_name);
	void read_signals(RailNetwork* network, string path_name);
	void read_signal_constraints(RailNetwork* network, string path_name);
	void read_speed_limits(RailNetwork* network, string path_name);
	void read_violations(RailNetwork* network, string path_name);
	void read_priorities(RailNetwork* network, string path_name);
	void read_parameters(RailNetwork* network, string path_name);

	void write_sections(RailNetwork* network, string path_name);

	// The whole rail network: nodes, segments, sections, terminals, arcs,
	// and the SignalsManager. Built from CSV in initialise()/process_network()
	// — IDs are assigned at construction and preserved across reset(), so
	// generated constraints stored as IDs (in deadlock_avoidance) resolve
	// correctly against any rebuilt network with the same input files.
	//
	// reset(Sim&) recreates the run-time SignalsManager and clears per-arc
	// state without rereading CSVs. Used by RailSimMaster::reset() to start
	// a new iteration of an experiment cheaply.
	class RailNetwork {
	public:
		vector<TrackNode*> nodes;
		vector<TrackSegment*> segments;
		vector<TrackSection*> sections;	//populated by initialise_network
		vector<Terminal*> terminals;
		map<string, Terminal*> terminal_map;
		vector<set<int>> violations;	//for each segment id, gives the set of adjacent segments on accute angles (i.e. violate switch configuration)
		vector<DirectedSegment*> arcs;	//populated by initialise_network
		SignalsManager* signals_mgr;

		double release_delay;			//delay applied before setting a TrackSegment to FREE (default 1s)

		RailNetwork(int n_nodes, int n_segments);
		~RailNetwork();

		void add_segment(int id, int node1, int node2, double length, bool bidirectional=true, bool disabled=false);
		TrackSection* add_section();	//adds and returns a new section, not populated with links (should not need to use if initialise_network is called)
		Terminal* add_terminal(string name);	//adds and returns a new terminal
		void add_violation(int segment1_id, int segment2_id);
		void initialise_network();	//some basic steps required after populating network data
		void process_network(Sim &sim);	//conducts depth first search to populate sections and reachability

		void record_warning(Train* train, DirectedSegment* arc, string warning);
	
		void reset(Sim& new_sim); //call this to reuse network in a subsequent simulation, resets to initial state
	private:
		static bool write_warnings_to_screen;
		static bool write_warnings_log;
		lfu::LogFile warnings_log;

		bool is_violation(int segment1_id, int segment2_id);
		void get_sections(DirectedSegment* start, DirectedSegment* arc, list<DirectedSegment*>& path, vector<bool>& visiting, vector<bool>& finished);
		void finalise_sections();
		void back_propogate_reach(Terminal* terminal, TrackSection* s, vector<bool>& visiting, vector<bool>& finished);
		void update_network_state(TrackSegment* segment, TrackSegment::TrackState state);
	};//RailNetwork

	class RollingStock {
	public:
		int id;
		string model;		//type of rail car or loco
		double length;

		RollingStock(string _model, double _length);

		static int rolling_stock_counter;
	};//RollingStock

	class TrainUpdateTrigger : public SimObject {
	public:
		enum UpdateType {
			FRONT_NODE,
			BACK_NODE,
			ACCEL_CHANGE,
			REQUEST_SECTION_PT,
			SECTION_GRANTED,
			SECTION_END,
			_COUNT
		};

		Train* train;
		UpdateType update_type;
		double delay_duration;
		double trigger_time;
		bool cancel_trigger;

		TrainUpdateTrigger(Train* _train, UpdateType _update_type, double delay_time = -10);
		void activate_trigger();
		SimCoroutine run() override;
	};//TrainUpdateTrigger

	class DelayedTrackRelease : public SimObject {
	public:
		Train* train;
		DirectedSegment* arc;
		double duration;
		SignalsManager* signals_mgr;

		DelayedTrackRelease(Train* _train, DirectedSegment* _arc);
		SimCoroutine run() override;
	};//DelayedTrackRelease

	class SpeedProfileArc {
	public:
		double d;		//distance from starting location of speed profile
		double length;	//length of ruling speed limit or arc
		DirectedSegment* arc;	//arc on which the speed change will occur
		double pos_start;	//position (to head node) where speed profile starts for this arc
		double pos_end;		//psotion where speed profile ends for this arc

		int seq;		//position in sequence of speed profile arcs
		double v0;		//speed at start of speed change or arc
		double v_max;	//speed of coasting phase (or maximum speed obtained before switching from accel to decel)
		double v1;		//speed at start of next speed change or arc

		double accel;	//acceleration and deceleration of train (NOTE: in future this can be arc specific)
		double decel;	//this will be -ve value

		double t;		//time required to traverse speed change or arc
		double t_accel;	//time in acceleration phase
		double t_coast;	//time in coasting phase
		double t_decel;	//time in deceleration phase (t = t_accel + t_coast + t_decel)
		double t0;		//time since start of speed profile reaching start of arc
		double t1;		//time since start of speed profile reaching end of arc (t1 = t0 + t)
	
		bool has_accel;	//boolean flags for each of the 3 phases
		bool has_coast;
		bool has_decel;

		double pos_coast;	//position (to head node) that costing starts after acceleration (arc_length + eps if all acceleration)
		double pos_decel;	//position toat deceleration starts (arc_length + eps if no decel)

		void set_parameters(Train* train, DirectedSegment* _arc, int _seq, double _d, double _pos_start, double _pos_end);
		double travel_time(double st_pos, double end_pos);
		void set_phases(double _v0, double _t0, double accel_dist, double coast_dist, double decel_dist);
		inline double accel_time(double v_st, double a, double dist);
		double get_next_accel_change_pos(double front_pos);
		double get_inst_speed(double front_pos);
	};//SpeedProfileArc

	extern LogFile* mlog_master;

	// A train. Itself a SimObject and therefore a coroutine — its run()
	// implements the full per-train state machine: request next section
	// from SignalsManager, compute speed profile over the granted arcs,
	// traverse them, dwell at terminals, repeat.
	//
	// run() spawns helper coroutines (TrainUpdateTrigger for motion update
	// events, DelayedTrackRelease for the trailing-edge of segment release)
	// and awaits them. Locals across co_await are preserved automatically
	// by the compiler in the coroutine frame.
	//
	// Note: Train::eps is the floating-point tolerance used in nearly every
	// motion comparison — never compare doubles raw, use the helpers from
	// comparison_tolerance.h with this epsilon.
	class Train : public SimObject {
	public:
		int id;

		//generally parameters but potentially dynamic variables
		bool persistent;				//indicates whether the train persists or is destroyed after its first job
		double length;
		double max_speed;				//units distance / unit time (e.g. km/hr)
		double accel;					//average acceleration
		double decel;					//average deceleration
		vector<RollingStock*> cars;		//ordered list of rolling stock making up train, front to back
		
		TrainManagerBase* manager;
		RailNetwork* network;
		SignalsManager* signals_mgr;
		vector<TrainUpdateTrigger*> updates;		//sub-processes to wake up train when something happens

		//dynamic variables

		//variables relating to train tasks
		Job* job;						//current list of destinations to visit and associated tasks

		//variables relating to train position
		bool forward;					//true indicates the first car is at front in direction of travel, false means it's at the back
		list<DirectedSegment*> arcs;	//set of arcs occupied by train
		list<DirectedSegment*> locked;	//set of downstream arcs locked (not occupied by train)
		double front_pos;				//distance to the head node of arcs.front() where the front of the train is located
		double back_pos;				//distance to the head node of arcs.back() where the back of the train is located
		double v_inst;					//most recent instantaneous speed calc
		double current_acceleration;	//current acceleration \in {-decel,0,accel}
		double current_speed_limit;
		vector<SpeedProfileArc> speed_profile;
		SpeedProfileArc* sp_arc_ptr;	//current speed profile arc
		Terminal* target;				//current terminal targetted by the train
		TrackSection* section;			//current section granted to the train
		TrackSection* new_section;
		TrainUpdateTrigger* live_update;		//current state updates that require action
		bool finished;
		double spawn_scheduled_time = 0.0;  // set by spawner before activate()

		lfu::LogFile *log;
		lfu::LogFile *mlog;		//log file for train performance metrics

		Train(string _descr, Sim& _sim, 
			bool _persistent, double _max_speed, double _accel, double _decel, 
			RailNetwork* _network, TrainManagerBase* _manager= nullptr);
		~Train();

		void add_car(RollingStock* car);
		void delete_cars();
		void place(DirectedSegment* arc, double _front_pos);

		SimCoroutine run() override;

		//functions only accessed internally by other "friend" classes
		void assign(Job* _job);		//called by manager
		void notify_section_granted(TrackSection* _new_section);	//called by signals_mgr
		void action_update_trigger(TrainUpdateTrigger* update_trigger);	//called by TrainUpdateTrigger

		static bool write_train_log;
		static bool write_log_to_screen;
		static bool write_metric_log;
		static bool write_metric_to_screen;
		static double eps;		//epsilon for numerical tolerance

		static int train_counter;		//train counter used to set id for each train

	private:
		double cumulative_distance;		//just for stats collection
		
		//used for logging purposes
		typedef enum {REQJOB,GETJOB,REQSEC,GETSEC,TRAVEL,STDWL,ENDDWL,ENDJOB} LogPhase;
		double vinst_prev;
		double t_prev;

		void init_log();
		string occupancy_str(bool forward_locked= false);
		void log_entry();
		void mlog_update(LogPhase phase);

		double get_speed_profile();
		double get_transit_time(double v0, double a, double d);		//solve d=v0t+1/2at^s for t given d and v0
		void set_update_trigger(TrainUpdateTrigger::UpdateType update_type, double delay_time = -10);
		void cancel_update_trigger(TrainUpdateTrigger::UpdateType update_type);
		void update_motion();
	};//Train

	//defines a particular terminal, dwell time (and potentially other duties) for a train job
	class Task {
	public:
		int id;
		Terminal* terminal;
		bool stop;
		double dwell_time;

		Task(Terminal* _terminal, bool _stop, double _dwell_time);
		Task(const Task& rhs);
		Task();

		static int task_counter;
	};//Task

	//this class defines a job to be done by a train based on where it needs to go
	//
	class Job {
	public:
		int id;
		vector<Task*> tasks;	//popuated externaly to this class

		Job();
		~Job();

		void add_task(Terminal* terminal, bool stop, double dwell_time);

		static int job_counter;
	};//Job

	//base class for dispatching of trains
	class TrainManagerBase : public SimObject {
	public:
		RailNetwork* network;
		list<Train*> ready_trains;

		TrainManagerBase(Sim& _sim, RailNetwork* _network);
		void ready_signal(Train* train);
		SimCoroutine run() override;

	protected:
		Job* create_job(Train* train);	//overridable function for creating a job for the supplied train
	};//TrainManagerBase

	//base class for network manager for granting trains access to sections
	// Gatekeeper for section access. Trains call request_access() with a
	// target terminal and an entry arc; if the request can be granted
	// immediately (section available, target reachable, no constraint
	// violated), it is. Otherwise it is queued, and when segments free up
	// the manager wakes and re-evaluates queued requests in priority order.
	//
	// grant_access() consults three things in turn:
	//   1) TrackSection::is_available() and reachable[target]
	//   2) Every manual SignalConstraint registered on the entry arc
	//      (is_violated, arc-level)
	//   3) Every generated SignalConstraint (is_violated_seg, segment-level)
	//
	// SignalsManager is itself a SimObject coroutine — it sleeps until
	// woken by track-release events, then iterates its request queue.
	class SignalsManager : public SimObject {
	public:
		RailNetwork* network;
		set<Train*> active_trains;

		struct AccessRequest {
			Train* train;
			Terminal* target;
			DirectedSegment* from_arc;
			double request_time;

			AccessRequest(Train* _train, Terminal* _target, DirectedSegment* _from_arc, double _request_time);
		};

		SignalsManager(Sim& _sim, RailNetwork* _network);
		TrackSection* request_access(Train* train, Terminal* target, DirectedSegment* from_arc);		//returns nullptr if no sections available
		void notify_segment_free();
		void notify_active_train(Train* train);
		void notify_inactive_train(Train* train);
		SimCoroutine run() override;

		const list<AccessRequest*>& get_access_requests() const;
		bool is_deadlocked() const;

		void reset();	//re-initialise for new simulation

	private:
		list<SignalsManager::AccessRequest*> access_requests;

		TrackSection* grant_access(Train* train, Terminal* target, DirectedSegment* from_arc);
	};//SignalsManager

	//this class allows for definition of constraints on track occupation (usually to prevent deadlocks)
	// An occupancy constraint defined on a set of arcs (manual constraints
	// from signal_constraints.csv) or segments (generated constraints from
	// the deadlock learner). is_violated() is the predicate consulted by
	// SignalsManager::grant_access(): a constraint blocks a request if
	// granting it would push the count of distinct trains over occupancy_limit.
	//
	// Two evaluation paths exist:
	//   - is_violated()      — arc-level, used by manual constraints.
	//   - is_violated_seg()  — segment-level, used by generated constraints.
	//                          Tuple is (segment, front_signal, target);
	//                          assumes trains with a given target only occupy
	//                          a segment in one direction.
	//
	// Target matching: when target is non-null, only trains heading to that
	// terminal count toward the occupancy. This is how the learner generates
	// route-specific constraints without globally restricting traffic.
	class SignalConstraint {
	public:
		string str_id;
		int occupancy_limit;		//maximum allowed number of matching trains occupying these arcs

		struct ConstraintArc {
			DirectedSegment* arc;
			Terminal* target;
			DirectedSegment* front_signal;	// required front signal position (nullptr for manual constraints)

			ConstraintArc(DirectedSegment* _arc, Terminal* _target, DirectedSegment* _front_signal = nullptr) :
				arc(_arc), target(_target), front_signal(_front_signal) {}
		}; //NOTE: can have multiple of the same arc with different targets
		list<ConstraintArc*> c_arcs;

		// Segment-level tuples — deduplicated from c_arcs (generated constraints only)
		struct SegTuple {
			TrackSegment* segment;
			DirectedSegment* front_signal;
			Terminal* target;
			double min_length = 0.0;  // minimum train length to qualify (0 = any)
			double max_length = 0.0;  // maximum train length to qualify (0 = no limit)
		};
		vector<SegTuple> seg_tuples;

		SignalConstraint(string _str_id, RailNetwork* network, int _occupancy_limit) :
			str_id(_str_id), occupancy_limit(_occupancy_limit) {}

		~SignalConstraint() {
			for (auto sc : c_arcs)
				delete sc;
			c_arcs.clear();
		}

		void add_constraint_arc(DirectedSegment* _arc, Terminal* _target, DirectedSegment* _front_signal = nullptr);

		bool is_violated(DirectedSegment* _arc_req, Train* _train, TrackSection* _section = nullptr);
		bool is_violated_seg(DirectedSegment* _arc_req, Train* _train, TrackSection* _section = nullptr);

		static bool verify_seg_mode;	// when true, grant_access() dual-evaluates arc vs seg
		static bool log_fires;			// when true, log each generated constraint fire
		static FILE* fire_log;			// file handle for constraint fire log

		static bool is_target_match_seg(Terminal* constraint_target, Train* train);
		static bool tail_clears_segment(Train* train, TrackSegment* target_seg, double tail_behind);

	private:
		bool is_target_match(ConstraintArc* c_arc, Train* train);
	};//SignalConstraint

}//DesRail namespace