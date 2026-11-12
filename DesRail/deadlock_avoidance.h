#pragma once

#include <vector>
#include <string>
#include <map>
#include <set>
#include <queue>
#include <stack>
#include <fstream>
#include "desrail.h"
#include "simulator.h"

using namespace std;
using namespace DesRail;

namespace DesRailDL {

	// A signal constraint produced by the deadlock learner, in serializable
	// form. Stored as IDs (segment, terminal name, front-signal arc) rather
	// than pointers, so it survives a network rebuild between iterations
	// of the iterative learning loop. apply_generated_constraints() reads
	// these back into live SignalConstraint objects on a freshly-built
	// network. write/read_generated_constraints handle the CSV round-trip
	// (format documented in docs/csv_schemas.md under generated_constraints).
	//
	// A5: FootprintSpec is train-level. One spec per core member, carrying the
	// head-anchored arc sequence that member occupied, its destination qualifier
	// and its length. The old SegTargetSpec was segment-level, so a train
	// occupying two of a constraint's segments produced two specs naming the same
	// train (comment 2.12). This is the canonical form for learner output;
	// manual constraints from signal_constraints.csv use a different arc-level form.
	struct GeneratedConstraint {
		string str_id;
		int occupancy_limit;

		struct FootprintSpec {
			// F as (segment_id, head_node_id) pairs, head-anchored: arcs[0] is the
			// front-signal arc, later entries run back along the route.
			vector<pair<int, int>> arcs;
			string terminal_name;	// "NA" for any target
			double min_length = 0.0;  // minimum train length to qualify (0 = any)
		};
		vector<FootprintSpec> seg_specs;
	};

	// Constraint-file format marker. A5 changed the tuple from segment-level to
	// train-level footprints, so a pre-A5 constraints.csv cannot be read as if it
	// were current: the reader rejects it by name rather than misinterpreting the
	// columns. Bump this if the format changes again.
	extern const char* CONSTRAINT_FILE_HEADER;

	// Conflict graph node
	struct ConflictNode {
		int id;
		Train* train;				// nullptr for omega
		DirectedSegment* waiting_for_arc;
		Terminal* target;
		bool is_omega;

		ConflictNode(int _id, Train* _train, DirectedSegment* _waiting_for_arc, Terminal* _target)
			: id(_id), train(_train), waiting_for_arc(_waiting_for_arc), target(_target), is_omega(false) {}
	};

	// Directed graph of "X is blocked by Y" relationships among trains in
	// a deadlocked simulation state. One node per blocked train, plus an
	// "omega" sink representing trains that are still progressing. Edges
	// are labelled with the type of block (S: physical via track ownership,
	// C: constraint-induced) and carry the underlying blocking arcs for
	// later constraint generation.
	//
	// build_from_state() populates the graph by scanning every blocked
	// section request held by SignalsManager. The result feeds into
	// tarjan_scc() — non-singleton SCCs that cannot reach omega are
	// the deadlock cycles we want to break.
	// A blocking "reason" for one candidate section: a set of trains that are
	// jointly present (AND). A physical occupant is a singleton reason; a violated
	// constraint is the reason {its tuple-trains}. A section is blocked iff SOME
	// reason is fully present (OR across reasons); it is passable iff EVERY reason
	// is defeated (each has >=1 train that can reach omega). cid is "" for physical.
	struct BlockReason {
		string cid;
		vector<pair<int, DirectedSegment*>> trains;	// (blocker node, blocking segment arc)
		// Arcs of the REQUESTING train's own occupancy that help this constraint
		// fire (the tuple anchored at the requested position is matched against the
		// requester itself). Needed because the cut must pin them: a variant train
		// with the same head, destination and length but a different approach path
		// would match the cut while not satisfying this constraint, so the cut would
		// deny a state that is not doomed. This is Gap A of
		// revision/length_monotonicity_proof.md, and recording these arcs closes it
		// by construction. Empty for a physical reason.
		vector<DirectedSegment*> self_arcs;
	};
	// One candidate (blocked) section of a requesting train.
	struct CandidateSection {
		string sec_id;
		vector<BlockReason> reasons;
	};

	class ConflictGraph {
	public:
		vector<ConflictNode*> nodes;
		vector<vector<int>> adjacency;
		map<pair<int,int>, vector<string>> edge_labels;	// (from,to) -> all blocking context labels
		map<pair<int,int>, vector<DirectedSegment*>> edge_blocking_arcs;	// (from,to) -> all blocking arcs
		// Per requesting node: its blocked candidate sections, each with the full
		// set of blocking reasons. Authoritative source for the free/stuck fixpoint,
		// for internal-coverage pruning, and for per-section constraint generation.
		map<int, vector<CandidateSection>> node_sections;
		ConflictNode* omega;
		map<Train*, ConflictNode*> train_to_node;

		ConflictGraph();
		~ConflictGraph();

		void build_from_state(SignalsManager* signals_mgr, RailNetwork* network);

		// Nodes that can reach omega under AND-OR (reason-based) reachability:
		// a node is free iff it has a passable section (every reason defeated).
		// Edges to free nodes are "ghosts" — ignored for SCC and pruning.
		set<int> compute_free_nodes() const;

	private:
		ConflictNode* get_or_create_node(Train* train, DirectedSegment* from_arc, Terminal* target);
		void add_edge(int from, int to, string label = "");
	};

	// Tarjan's SCC result
	struct SCCResult {
		vector<vector<int>> components;		// each component is a list of node indices
	};

	SCCResult tarjan_scc(const vector<vector<int>>& adjacency, int n);

	// Orchestrates the deadlock-detection-to-constraint-generation pipeline:
	//
	//   1. Build a ConflictGraph from current SignalsManager state.
	//   2. Run Tarjan's SCC; identify deadlock SCCs (non-singleton, no path
	//      to omega).
	//   3. Filter to root-cause SCCs (sinks in the condensed DAG) so we
	//      don't generate constraints for derivative deadlocks.
	//   4. For each root-cause SCC, run prune_to_minimal_core to cut it down
	//      to a minimum self-contained core.
	//   5. Generate one GeneratedConstraint per pruned SCC via
	//      generate_constraint_from_scc.
	//
	// One DeadlockAnalyzer instance per analysis pass. Constructed by
	// DeadlockMonitor when it detects a candidate stall; results read out
	// via get_generated_constraints().
	class DeadlockAnalyzer {
	public:
		DeadlockAnalyzer(SignalsManager* _signals_mgr, RailNetwork* _network);

		bool analyze();		// returns true if deadlock detected
		vector<GeneratedConstraint> get_generated_constraints() const;

		bool write_dot;		// if true, export DOT file on deadlock detection
		string dot_path;	// output path for DOT file
		vector<string> prune_log;	// per-SCC pruning summaries (e.g. "4>3[-dl_11]")
		// Global toggle for pruning to a minimal self-contained core. Default on; set
		// to false via the PRUNING option in dlexp_options.csv to ablate pruning.
		static bool pruning_enabled;
		// Expansion cap on the phase 2 core search (PRUNE_SEARCH_CAP in
		// dlexp_options.csv). The search certifies the core as a minimum when it
		// completes; on reaching the cap it returns the phase 1 core, which is
		// still self-contained but is not certified minimal. 0 runs phase 1
		// alone, which is the ablation of the search.
		static long prune_search_cap;
		// Counters over the run: cores whose minimality was proven, cores whose
		// search reached the cap, and trains removed by pruning in total.
		static long search_proven;
		static long search_capped;
		static long trains_pruned;
		ofstream* scc_log;	// if non-null, write SCC details to this file
		int iteration;		// current iteration number (for SCC log)

	private:
		SignalsManager* signals_mgr;
		RailNetwork* network;
		vector<GeneratedConstraint> generated_constraints;

		GeneratedConstraint generate_constraint_from_scc(
			ConflictGraph& graph, const vector<int>& scc);
		// Working state of the phase 2 search, carried down the recursion.
		struct CoreSearch {
			std::set<int> best;				// incumbent core
			std::set<std::set<int>> visited;	// sets already expanded
			long expansions = 0;
			long cap = 0;
			bool capped = false;
		};
		// First candidate section of a member of S that no reason lying inside S
		// covers. Returning false means every section of every member is covered
		// from within S, which is exactly self-containment. Enumeration order is
		// ascending node id then recorded section order: any order is correct,
		// and one is fixed so the Windows and Linux builds agree.
		bool uncovered_section(
			ConflictGraph& graph, const std::set<int>& S, int& node, int& sec_idx) const;
		// Phase 1. Grows one self-contained set outward from a seed train, taking
		// at each step the in-core reason that brings in fewest new trains.
		std::set<int> dive_core(
			ConflictGraph& graph, const std::set<int>& scc_set, int seed) const;
		// Phase 2. Branch and bound over the same step: proves the incumbent is a
		// minimum self-contained subset, or replaces it with a smaller one.
		void expand_core(
			ConflictGraph& graph, const std::set<int>& scc_set, CoreSearch& st,
			const std::set<int>& U) const;
		// Runs both phases from the root-cause SCC. The returned core is always
		// self-contained; it is a minimum, and so has no redundant subset, unless
		// the search reached PRUNE_SEARCH_CAP.
		vector<int> prune_to_minimal_core(
			ConflictGraph& graph, const vector<int>& scc, vector<string>& pruned_cids);
		// A set of trains is self-contained iff every candidate section of every
		// member has a blocking reason lying entirely within the set (Def:
		// Section coverage; self-contained set). Root-cause deadlock SCCs are
		// exactly the self-contained ones (Def: Root-Cause SCC).
		bool is_self_contained(ConflictGraph& graph, const std::set<int>& S) const;
	};

	// SimObject coroutine that watches for deadlocks during a simulation.
	// Runs off the hot path: every check_interval (sim time units), it
	// inspects all blocked section requests in SignalsManager, and if any
	// request has been blocked longer than deadlock_timeout it triggers a
	// DeadlockAnalyzer pass. Intentionally polling-based — avoids putting
	// any cost on grant_access() (the actual hot path).
	//
	// Adds zero overhead when no deadlock is suspected. The trade-off is
	// detection latency: a deadlock that forms at sim time T is caught
	// somewhere in (T+deadlock_timeout, T+deadlock_timeout+check_interval).
	class DeadlockMonitor : public SimObject {
	public:
		SignalsManager* signals_mgr;
		RailNetwork* network;
		double check_interval;
		double deadlock_timeout;
		bool deadlock_detected;
		vector<GeneratedConstraint> generated_constraints;
		vector<string> prune_log;
		bool write_dot;
		string dot_path;
		ofstream* scc_log;	// passed through to DeadlockAnalyzer
		int iteration;		// passed through to DeadlockAnalyzer
		bool verbose_dot;	// dump conflict graph even when no SCC found
		double start_warmdown = 0;	// drain: once past this with an empty network, stop (safe). 0 = off.

		DeadlockMonitor(Sim& _sim, SignalsManager* _signals_mgr, RailNetwork* _network,
			double _check_interval, double _deadlock_timeout);

		SimCoroutine run() override;
	};

	// Constraint application and export
	void apply_generated_constraints(RailNetwork* network, vector<GeneratedConstraint>& constraints,
		map<string, SignalConstraint*>* live_constraint_map = nullptr);
	void write_generated_constraints(vector<GeneratedConstraint>& constraints, string path);
	vector<GeneratedConstraint> read_generated_constraints(string path, string last_constraint = "NA");

	// Graph export for visualization
	void write_conflict_graph_dot(ConflictGraph& graph, SCCResult& scc, string path, double sim_time,
		const set<string>& pruned_constraint_ids = {}, const set<int>& pruned_nodes = {});

	// Counter for generating unique constraint IDs
	extern int generated_constraint_counter;

	// Deadlock avoidance experiment loop
	void run_deadlock_avoidance_exp();

}//DesRailDL namespace
