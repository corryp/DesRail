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
	// SegTargetSpec is segment-level: the constraint qualifies a segment
	// by (target terminal, front-signal arc), with optional length range.
	// This is the canonical form for learner output; manual constraints
	// from signal_constraints.csv use a different arc-level form.
	struct GeneratedConstraint {
		string str_id;
		int occupancy_limit;

		struct SegTargetSpec {
			int segment_id;
			string terminal_name;	// "NA" for any target
			int front_signal_segment_id;
			int front_signal_head_node_id;
			double min_length = 0.0;  // minimum train length to qualify (0 = any)
			double max_length = 0.0;  // maximum train length to qualify (0 = no limit)
		};
		vector<SegTargetSpec> seg_specs;
	};

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
	//   4. For each root-cause SCC, run prune_redundant_constraint_edges
	//      to remove edges whose removal doesn't shrink the SCC.
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
		// Global toggle for redundant constraint edge pruning. Default on; set
		// to false via the PRUNING option in dlexp_options.csv to ablate pruning.
		static bool pruning_enabled;
		ofstream* scc_log;	// if non-null, write SCC details to this file
		int iteration;		// current iteration number (for SCC log)

	private:
		SignalsManager* signals_mgr;
		RailNetwork* network;
		vector<GeneratedConstraint> generated_constraints;

		GeneratedConstraint generate_constraint_from_scc(
			ConflictGraph& graph, const vector<int>& scc);
		vector<int> prune_redundant_constraint_edges(
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
