#pragma once

#include <list>
#include <vector>
#include <cassert>
#include <string>
#include <map>
#include <set>
#include <queue>
#include <tuple>
#include "simulator.h"
#include "read_csv.h"
#include "log_file_util.h"
#include "env_util.h"

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
		bool is_transient;		//speed_limit applies only until the REAR of the train clears this arc (plus
								//however much further downstream is needed to reach train length), then the
								//ruling limit reverts to whatever it was before -- unlike an ordinary (persistent)
								//speed_limit, which keeps applying until the next speed-change arc is reached.
								//Ignored/false unless is_speed_change is also true.

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
	void read_violation_triplets(RailNetwork* network, string path_name);
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
		// Forbidden three-ARC sequences (violation_triplets.csv). `violations` are
		// pairwise between segments that SHARE A NODE. Where a flat crossing has been
		// modelled by splitting its node into two joined by a short dummy segment (so
		// that the crossing point becomes a lockable resource like any other segment),
		// the legs of the crossing no longer share a node, and no pair can express the
		// turns the railway cannot make: `a -> dummy -> d` is two individually legal
		// adjacencies. This is the same prohibition at arity 3. Matched only on three
		// CONSECUTIVE arcs -- a section that merely contains all three elsewhere is fine.
		set<tuple<int, int, int>> violation_triplets;	//keyed by DirectedSegment id
		vector<DirectedSegment*> arcs;	//populated by initialise_network
		SignalsManager* signals_mgr;

		double release_delay;			//delay applied before setting a TrackSegment to FREE (default 1s)

		// When true, a train standing at a terminal that cannot reach its next target
		// in the direction it faces will turn around in place, provided it CAN reach it
		// facing the other way (these are EMUs with a cab at each end: the driver walks
		// to the other end and nothing moves). Off by default, so every existing
		// scenario is bit-identical. The test is purely topological -- see
		// `can_reach` -- so congestion can never trigger a reversal, which is what
		// keeps the deadlock characterisation unchanged: reversal is a property of the
		// timetable, never an escape from contention.
		bool automatic_reverse_detect = false;

		// Which SignalsManager policy to construct (general_params `signals_manager`).
		// "base" is the classic manager and is the default, so every existing scenario
		// is bit-identical. Any other value is resolved by make_signals_manager(), which
		// THROWS on a name it does not know rather than silently falling back -- a typo
		// in a scenario must not quietly turn an experiment rung into a duplicate of the
		// control.
		string signals_manager = "base";

		RailNetwork(int n_nodes, int n_segments);
		~RailNetwork();

		void add_segment(int id, int node1, int node2, double length, bool bidirectional=true, bool disabled=false);
		TrackSection* add_section();	//adds and returns a new section, not populated with links (should not need to use if initialise_network is called)
		Terminal* add_terminal(string name);	//adds and returns a new terminal
		void add_violation(int segment1_id, int segment2_id);
		void add_violation_triplet(int arc1_id, int arc2_id, int arc3_id);
		// Can `target` be reached from `from_arc` at all? Availability and constraints
		// are deliberately EXCLUDED: this asks whether a route exists, not whether one
		// is free right now, so a congested train is never mistaken for a stranded one.
		bool can_reach(DirectedSegment* from_arc, Terminal* target) const;
		void initialise_network();	//some basic steps required after populating network data
		void process_network(Sim &sim);	//conducts depth first search to populate sections and reachability

		void record_warning(Train* train, DirectedSegment* arc, string warning);
	
		void reset(Sim& new_sim); //call this to reuse network in a subsequent simulation, resets to initial state
	private:
		static bool write_warnings_to_screen;
		static bool write_warnings_log;
		lfu::LogFile warnings_log;

		bool is_violation(int segment1_id, int segment2_id);
		bool is_violation_triplet(const DirectedSegment* a, const DirectedSegment* b,
			const DirectedSegment* c);
		bool triplet_spans_join(const vector<DirectedSegment*>& tail,
			const vector<DirectedSegment*>& head);
		//`a` and `b` are the two arcs preceding `c`; null `a` means `c` extends a
		//sequence only two arcs long, which no triplet can match.
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
			PROFILE_SPLIT,	//front reaches an internal sub-arc boundary within the SAME physical arc
							//(a transient speed limit's clearance zone), not a new arc -- see
							//SpeedProfileArc::continues_in_next. Unlike FRONT_NODE this must NOT touch
							//arcs/locked/current_speed_limit.
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

		//The two limits governing this span, reported separately so a train log states both
		//rather than only their minimum. Recorded here because reconstructing either from the
		//arc's own speed_limit is wrong wherever a limit is CARRIED across arcs that declare
		//none, which is most of a station throat.
		//  persistent_limit -- the prevailing limit the train carries into this span
		//  transient_limit  -- whatever restricts below it here, or 0 for none. It covers both
		//                      a transient speed limit and a prevailing zone whose tail the
		//                      train has not yet cleared, since the engine holds the two in
		//                      the same structure; separating them would mean tagging `active`
		//                      in get_speed_profile.
		//The speed actually ruling is the lower of the two, which is why both are logged: a
		//30 showing in one column while the other reads 100 says something quite different
		//from a 30 in both.
		double persistent_limit = 0;
		double transient_limit = 0;
	
		bool has_accel;	//boolean flags for each of the 3 phases
		bool has_coast;
		bool has_decel;

		double pos_coast;	//position (to head node) that costing starts after acceleration (arc_length + eps if all acceleration)
		double pos_decel;	//position toat deceleration starts (arc_length + eps if no decel)

		bool continues_in_next;	//true if speed_profile[seq+1] is ANOTHER SUB-SPAN OF THE SAME PHYSICAL ARC
									//(a transient speed limit's clearance zone split this arc partway through),
									//not a new physical arc. Train::run must not wait for/expect a FRONT_NODE
									//event to advance past an entry with this set -- there is no node there.

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
		string train_group;				//closed-loop dispatch: only jobs of a matching train_group are assigned to this train
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
		int reversal_count = 0;			//bumped by reverse(); the animation watches it to rebuild the consist
		list<DirectedSegment*> arcs;	//set of arcs occupied by train

		//Transient speed limits this train is still clearing, as (clearance point, ceiling).
		//Persists across get_speed_profile() calls so a clearance zone that spans a section
		//boundary is remembered correctly; clearance points are in cumulative_distance's frame
		//(this trip), not any one profile's. More than one entry means overlapping restrictions --
		//two crossovers closer together than the train is long -- and the tightest in force wins.
		//What to revert TO is never stored: it is the ruling PERSISTENT limit, which
		//get_speed_profile tracks as it walks the arcs.
		vector<pair<double, double>> transient_pending;
		list<DirectedSegment*> locked;	//set of downstream arcs locked (not occupied by train)
		double front_pos;				//distance to the head node of arcs.front() where the front of the train is located
		double back_pos;				//distance to the head node of arcs.back() where the back of the train is located
		double v_inst;					//most recent instantaneous speed calc
		double current_acceleration;	//current acceleration \in {-decel,0,accel}
		double current_speed_limit;
		//The prevailing limit the train is carrying, and any transient restricting below it
		//(0 for none). Kept as scalars rather than read
		//from sp_arc_ptr at log time: that pointer is null before the first profile and dangles
		//after get_speed_profile rebuilds the vector, which segfaulted when log_entry first
		//tried to dereference it.
		double persistent_limit_now = 0;
		double transient_limit_now = 0;
		vector<SpeedProfileArc> speed_profile;
		SpeedProfileArc* sp_arc_ptr;	//current speed profile arc
		Terminal* target = nullptr;		//current terminal targetted by the train (init: unset until the train gets a job; read in log_entry before assignment on Linux gave a garbage non-null deref)
		TrackSection* section;			//current section granted to the train
		TrackSection* new_section;
		TrainUpdateTrigger* live_update;		//current state updates that require action
		bool finished;
		double spawn_scheduled_time = 0.0;  // set by spawner before activate()
		int trip_seq = 0;                   // per-train trip counter (1,2,... ); persistent trains chain many trips ("job" in the DB, "trip" in results)
		// Index into job->tasks of the task in progress, -1 when there is no job.
		// Train::run's own loop counter is a local, so without this the signals manager
		// cannot see which task a request belongs to, nor what the NEXT call is -- and
		// the onward guard of a rerouting policy is a question about the next call.
		int task_idx = -1;

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
		// One row per SECTION a train traverses: entry, any stop within it, exit, the grant of
		// the following section, and the terminal it stood at. Signal-to-signal blocks are the
		// natural unit for occupancy and capacity work, and station-to-station running time is
		// a sum over them, so this replaces `train_log` for anything that only needs times. It
		// is ~1,500 rows on inner Brisbane against ~30,000 for train_log, and costs nothing
		// measurable (0.90 s vs 0.89 s over 3 reps with log_output off). Switched by
		// `section_log` in runctrl.csv, independently of `log_output`.
		//
		// entry_time / exit_time both mean "the head reached the head node of an arc": entry is
		// the crossing into the section's first arc, exit the crossing out of its last. That is
		// deliberately NOT the grant of the next section -- a train that ran straight through is
		// granted well before it exits, and one that had to stand at the section-end signal is
		// granted after it stopped but before it exits. Keeping the two apart is what makes run
		// times over a section comparable across free-running, slowed and stopped traversals.
		// next_grant_time is -1 when no successor was ever granted (the last section of a job,
		// and the inbound section of a turnback, which is closed by the reversal itself). On
		// that final section exit_time is the despawn instant rather than a node crossing,
		// since nothing pushes the head out of it.
		static bool write_section_log;
		static double eps;		//epsilon for numerical tolerance

		static int train_counter;		//train counter used to set id for each train

	private:
		LogFile* seclog = nullptr;
		TrackSection* sec_cur = nullptr;	//section currently being timed for seclog
		double sec_entry_t = 0;
		double sec_grant_t = -1;			//when the following section was granted
		DirectedSegment* sec_last_arc = nullptr;	//last arc of sec_cur; leaving it is the exit
		DirectedSegment* prev_front_arc = nullptr;	//front arc immediately before the last advance
		double sec_stop_t = -1;				//when the train came to rest in this section, -1 if it did not
		double sec_stop_dur = 0;
		Terminal* sec_terminal = nullptr;	//terminal it stood at, if any

		double cumulative_distance;		//just for stats collection
		
		//used for logging purposes
		typedef enum {REQJOB,GETJOB,REQSEC,GETSEC,TRAVEL,STDWL,ENDDWL,ENDJOB} LogPhase;
		double vinst_prev;
		double t_prev;

		void init_log();
		string occupancy_str(bool forward_locked= false);

		// Turn the train around where it stands. Nothing moves: the same segments stay
		// occupied and owned, so there is no lock to transfer and nothing to release.
		// Only which end leads changes -- every occupied arc is swapped for its
		// opposite and the list reversed, and the positions are complemented.
		void reverse();

		// Called at the start of each task: turn around if the target cannot be reached
		// facing this way but can be facing the other. No-op unless
		// network->automatic_reverse_detect is set.
		void maybe_reverse();
		void log_entry();
		void mlog_update(LogPhase phase);
		// Emit the pending section row and begin timing `sec`. Passing nullptr just flushes,
		// which is what job completion and train destruction need.
		// The section log is driven by where the FRONT physically is, not by grants. A train is
		// granted its next section before it reaches the boundary, so a grant-driven row cannot
		// carry a true exit time -- and exit is what makes section traversal times comparable
		// across trains that ran through, entered slow, or stopped at the far end.
		void section_log_enter(TrackSection* sec);   // front entered `sec`
		void section_log_exit();                     // front left the last arc of the current section
		void section_log_grant(TrackSection* granted);// the FOLLOWING section was granted
		void section_log_front_advanced();           // call AFTER the front arc changes

		double get_speed_profile();
		double get_transit_time(double v0, double a, double d);		//solve d=v0t+1/2at^s for t given d and v0
		void set_update_trigger(TrainUpdateTrigger::UpdateType update_type, double delay_time = -10);
		void cancel_update_trigger(TrainUpdateTrigger::UpdateType update_type);
		//Cancel every trigger that belongs to the CURRENT speed profile. Call this wherever the
		//profile is abandoned before it runs out, so a trigger armed against the old profile
		//cannot fire against the new one. It exists so that adding a motion trigger means
		//editing one list rather than remembering every abandon site -- PROFILE_SPLIT was added
		//without updating them and a stale one corrupted front_pos/back_pos (see the comment on
		//UpdateType::PROFILE_SPLIT).
		void cancel_motion_triggers();
		void update_motion();
	};//Train

	//defines a particular terminal, dwell time (and potentially other duties) for a train job
	class Task {
	public:
		int id;
		Terminal* terminal;
		bool stop;
		double dwell_time;		//MINIMUM stand at the terminal
		//Absolute sim time this task may not depart before: the service's booked
		//departure. A train that runs early holds here until its slot instead of
		//leaving on minimum dwell; one that is already late is unaffected, because
		//its slot has passed -- which is what a timetable means, and is why this is
		//absolute rather than measured from the spawn. <0 = no booked time, in which
		//case the task behaves exactly as it always has.
		double earliest_departure = -1.0;
		// The terminal this task was BOOKED at, fixed at construction. A routing policy
		// that substitutes a destination overwrites `terminal` (it has to: Train::run
		// exits its per-task loop on `arcs.front()->terminal == task->terminal`), so
		// without this the timetabled platform is gone and there is nothing left to
		// compare an actual against.
		Terminal* booked = nullptr;

		Task(Terminal* _terminal, bool _stop, double _dwell_time, double _earliest_departure = -1.0);
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
		string train_group;		//closed-loop dispatch: job is only allocated to a train of a matching group
		//Identity of the service this job realises, carried through to train_metric_log so a
		//simulated trip can be joined back to the row that generated it. For the inner-Brisbane
		//GTFS scenarios the spawner name IS the GTFS trip_id, which makes the metric log
		//joinable directly against observed stop_events.csv. Empty when unnamed.
		string service_name;
		double creation_time = 0.0;	//sim time the job was created (for job_to_train_wait metric)

		Job();
		~Job();

		void add_task(Terminal* terminal, bool stop = false, double dwell_time = 0,
			double earliest_departure = -1.0);

		static int job_counter;
	};//Job

	//base class for dispatching of trains.
	// Closed-loop dispatch is a two-queue matcher: persistent trains that have
	// finished a job announce themselves via ready_signal() (queued in
	// ready_trains); job spawners announce work via submit_job() (queued in
	// pending_jobs). run() drains all compatible (train, job) pairs -- FIFO,
	// matched by exact train_group -- assigning each job to a waiting train.
	// A train or job with no current counterpart stays queued until one arrives.
	class TrainManagerBase : public SimObject {
	public:
		RailNetwork* network;
		list<Train*> ready_trains;		//persistent trains awaiting a job
		list<Job*> pending_jobs;		//submitted jobs awaiting a compatible train

		TrainManagerBase(Sim& _sim, RailNetwork* _network);
		void ready_signal(Train* train);	//a train is ready for (another) job
		void submit_job(Job* job);			//a job spawner offers a job to the pool
		SimCoroutine run() override;

	private:
		Job* take_matching_job(const string& group);	//pop oldest pending job of that group, else nullptr
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

	protected:
		// ---- routing-policy extension points -------------------------------------
		// The base pair below IS the classic behaviour, so a plain SignalsManager is
		// unchanged. A derived manager overrides them to vary WHICH destination a
		// section may serve, without duplicating grant_access's availability test,
		// constraint evaluation, logging and locking -- the parts that must not fork.
		//
		// section_serves: may `section` be granted to `train`, which asked for
		// `target`? Sets `matched` to the destination it actually serves, which the
		// base always reports as `target` itself.
		//
		// on_granted: called once, after the section is locked and immediately before
		// grant_access returns it, on BOTH grant paths (the direct one and the deferred
		// one driven from run()). A policy that substitutes a destination must retarget
		// the train HERE: Train::run exits its per-task loop on
		// `arcs.front()->terminal == task->terminal` (desrail.cpp), so a train sent
		// somewhere its task does not name would never dwell, and the deferred path
		// never returns through Train's own call site. `target` is what the train asked
		// for, which is NOT always its current task's terminal: a train passing through
		// a zero-dwell call requests toward the next task before advancing to it.
		virtual bool section_serves(TrackSection* section, Terminal* target,
			Train* train, DirectedSegment* from_arc, Terminal*& matched) const;
		virtual void on_granted(Train* train, TrackSection* section, Terminal* target, Terminal* matched);

	private:
		list<SignalsManager::AccessRequest*> access_requests;

		TrackSection* grant_access(Train* train, Terminal* target, DirectedSegment* from_arc);
	};//SignalsManager

	// Construct the SignalsManager named by `network->signals_manager`. Defined in
	// signals_manager_factory.cpp, which is the ONLY core file that knows the derived
	// policies exist; desrail.cpp just calls this.
	SignalsManager* make_signals_manager(Sim& sim, RailNetwork* network);

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